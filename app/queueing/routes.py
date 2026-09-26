"""HTTP surface of the walk-in queue.

Public (no account): ``/api/public/<slug>/queue...`` - see what the queue
looks like, join it, check your place, leave it. Same protections as public
booking (app/public_booking/routes.py): the tenant must be live, the write is
CAPTCHA-gated, and every route is rate-limited - keyed per client IP *per
business*, so one busy forecourt's shared Wi-Fi can't exhaust another
business's limit.

The customer's bearer token is posted in the request body, never put in a
URL path or query string, for the same reason the deposit recovery token is
(it would otherwise land in proxy and access logs). The customer's link keeps
it in the URL fragment, which browsers don't transmit.

Staff: ``/api/queue...`` - garage-scoped from the JWT like every other staff
route. Operating the queue (open/close, call, check in, complete) is open to
any employee; changing its configuration is OWNER-only, matching
app/garages/schedule/routes.py.
"""

from flask import current_app, request
from flask.views import MethodView
from flask_jwt_extended import jwt_required
from flask_limiter.util import get_remote_address
from flask_smorest import Blueprint, abort

from app.auth.decorators import owner_required
from app.auth.utils import get_current_employee
from app.extensions import db, limiter
from app.models.appointments.appointment_type import GarageAppointmentType
from app.models.garage import Garage
from app.models.garage_schedule import GarageScheduleSettings
from app.models.queueing.queue_entry import QUEUE_WAITING, QueueEntry
from app.models.queueing.queue_settings import AVERAGE_MODE_MANUAL
from app.models.queueing.reserved_window import WalkInReservedWindow
from app.public_booking.captcha import verify_captcha
from app.public_booking.routes import _get_garage_by_slug

from . import service
from .eta import wait_minutes
from .schemas import (
    PublicQueueInfoSchema,
    PublicQueueStatusSchema,
    QueueDashboardSchema,
    QueueEntrySchema,
    QueueJoinedSchema,
    QueueJoinSchema,
    QueueReorderSchema,
    QueueSettingsSchema,
    QueueTokenSchema,
    ReservedWindowSchema,
    StartServiceSchema,
)

public_queue_blp = Blueprint(
    "public_queue",
    "public_queue",
    url_prefix="/api/public",
    description="Unauthenticated walk-in queue - join, live position/ETA, leave.",
)

queue_blp = Blueprint(
    "queue",
    "queue",
    url_prefix="/api/queue",
    description="Staff live walk-in queue: call, check in, complete, reorder, and "
    "the queue's configuration.",
)

_AUTH_DOC: dict[str, list[dict[str, list[str]]]] = {"security": [{"bearerAuth": []}]}


def _per_garage_client_key() -> str:
    slug = (request.view_args or {}).get("slug", "")
    return f"{get_remote_address()}|{slug}"


# ---------------------------------------------------------------------------
# Payload builders
# ---------------------------------------------------------------------------


def _status_payload(garage: Garage, entry: QueueEntry, snapshot, settings) -> dict:
    estimate = snapshot.estimates.get(entry.id) if entry.status == QUEUE_WAITING else None
    start = estimate.start if estimate is not None else None
    return {
        "garage_name": garage.name,
        "queue_is_open": settings.is_open,
        "ticket_number": entry.ticket_number,
        "status": entry.status,
        "end_reason": entry.end_reason,
        "customer_first_name": entry.customer_first_name,
        "position": estimate.position if estimate is not None else None,
        "people_ahead": estimate.position - 1 if estimate is not None else None,
        "estimated_start_at": start,
        "estimated_wait_minutes": wait_minutes(start, snapshot.now),
        "fits_today": estimate.fits_today if estimate is not None else None,
        "called_at": entry.called_at,
        "call_expires_at": service.call_expires_at(entry, settings),
    }


def _entry_payload(entry: QueueEntry, snapshot, settings) -> dict:
    estimate = snapshot.estimates.get(entry.id) if entry.status == QUEUE_WAITING else None
    start = estimate.start if estimate is not None else None
    appt_type = entry.appointment_type
    return {
        "id": entry.id,
        "status": entry.status,
        "end_reason": entry.end_reason,
        "ticket_number": entry.ticket_number,
        "customer_first_name": entry.customer_first_name,
        "customer_last_name": entry.customer_last_name,
        "customer_phone": entry.customer_phone,
        "sms_opt_in": entry.sms_opt_in,
        "vehicle_registration": entry.vehicle_registration,
        "notes": entry.notes,
        "appointment_type_id": entry.appointment_type_id,
        "appointment_type_name": appt_type.name if appt_type is not None else None,
        "service_minutes": entry.service_minutes,
        "joined_at": entry.created_at,
        "called_at": entry.called_at,
        "started_at": entry.started_at,
        "ended_at": entry.ended_at,
        "appointment_id": entry.appointment_id,
        "position": estimate.position if estimate is not None else None,
        "estimated_start_at": start,
        "estimated_wait_minutes": wait_minutes(start, snapshot.now),
        "fits_today": estimate.fits_today if estimate is not None else None,
        "call_expires_at": service.call_expires_at(entry, settings),
    }


def _person(first, last) -> str:
    return " ".join(p for p in (first, last) if p)


def _dashboard_payload(garage: Garage) -> dict:
    settings = service.get_queue_settings(garage.id)
    snapshot = service.queue_snapshot(garage)
    average = service.average_info(garage, settings, snapshot.now)
    refusal = service.join_refusal_reason(settings, snapshot, average.effective_minutes)
    walk_in_appointment_ids = {e.appointment_id for e in snapshot.entries if e.appointment_id}
    new_joiner = snapshot.new_joiner
    return {
        "now": snapshot.now,
        "service_date": snapshot.service_date,
        "is_open": settings.is_open,
        "accepting_joins": refusal is None,
        "refusal_reason": refusal,
        "refusal_message": service.REFUSAL_MESSAGES.get(refusal) if refusal else None,
        "capacity": snapshot.capacity,
        "opens_at": snapshot.opens_at,
        "closes_at": snapshot.closes_at,
        "no_show_timeout_minutes": settings.no_show_timeout_minutes,
        "average": average,
        "new_joiner_estimated_start_at": new_joiner.start if new_joiner else None,
        "new_joiner_fits_today": bool(new_joiner and new_joiner.fits_today),
        "entries": [_entry_payload(e, snapshot, settings) for e in snapshot.entries],
        "appointments": [
            {
                "id": a.id,
                "start_time": a.start_time,
                "end_time": a.end_time,
                "status": a.status,
                "customer_name": _person(a.customer.first_name, a.customer.last_name),
                "appointment_type_name": a.appointment_type_name_at_booking
                or a.appointment_type.name,
                "employee_name": _person(a.employee.first_name, a.employee.last_name)
                or a.employee.email,
                "is_walk_in": a.id in walk_in_appointment_ids,
            }
            for a in snapshot.appointments
            if a.status != "CANCELLED"
        ],
    }


def _settings_payload(garage: Garage) -> dict:
    settings = service.get_queue_settings(garage.id)
    schedule = GarageScheduleSettings.query.filter_by(garage_id=garage.id).first()
    return {
        "is_open": settings.is_open,
        "average_mode": settings.average_mode,
        "manual_average_minutes": settings.manual_average_minutes,
        "no_show_timeout_minutes": settings.no_show_timeout_minutes,
        "default_appointment_type_id": settings.default_appointment_type_id,
        "average": service.average_info(garage, settings, service.utcnow()),
        "capacity": service.queue_capacity(garage),
        "capacity_per_slot": schedule.capacity_per_slot if schedule is not None else None,
    }


def _staff_garage() -> Garage:
    employee = get_current_employee()
    assert employee is not None
    return employee.garage


# ---------------------------------------------------------------------------
# Public routes
# ---------------------------------------------------------------------------


@public_queue_blp.route("/<slug>/queue")
class PublicQueueInfo(MethodView):
    @limiter.limit(
        lambda: current_app.config["PUBLIC_QUEUE_STATUS_RATELIMIT"],
        key_func=_per_garage_client_key,
    )
    @public_queue_blp.response(200, PublicQueueInfoSchema)
    def get(self, slug):
        garage = _get_garage_by_slug(slug)
        service.sweep_queue(garage)
        settings = service.get_queue_settings(garage.id)
        snapshot = service.queue_snapshot(garage)
        minutes = service.average_info(garage, settings, snapshot.now).effective_minutes
        refusal = service.join_refusal_reason(settings, snapshot, minutes)
        new_joiner = snapshot.new_joiner
        start = new_joiner.start if new_joiner is not None else None
        db.session.commit()
        return {
            "garage_name": garage.name,
            "is_open": settings.is_open,
            "accepting_joins": refusal is None,
            "refusal_reason": refusal,
            "refusal_message": service.REFUSAL_MESSAGES.get(refusal) if refusal else None,
            "waiting_count": len(snapshot.waiting),
            "estimated_start_at": start if refusal is None else None,
            "estimated_wait_minutes": wait_minutes(start, snapshot.now)
            if refusal is None
            else None,
            "opens_at": snapshot.opens_at,
            "closes_at": snapshot.closes_at,
        }


@public_queue_blp.route("/<slug>/queue/join")
class PublicQueueJoin(MethodView):
    @limiter.limit(
        lambda: current_app.config["PUBLIC_QUEUE_JOIN_RATELIMIT"],
        key_func=_per_garage_client_key,
    )
    @public_queue_blp.arguments(QueueJoinSchema)
    @public_queue_blp.response(201, QueueJoinedSchema)
    def post(self, data, slug):
        garage = _get_garage_by_slug(slug)
        if not verify_captcha(data.get("captcha_token")):
            abort(400, message="CAPTCHA verification failed.")
        entry, token = service.join_queue(garage, data)
        settings = service.get_queue_settings(garage.id)
        payload = _status_payload(garage, entry, service.queue_snapshot(garage), settings)
        payload["token"] = token
        return payload


def _entry_by_token(garage: Garage, token: str) -> QueueEntry:
    entry: QueueEntry | None = QueueEntry.query.filter_by(
        garage_id=garage.id, public_token_hash=service.hash_token(token)
    ).first()
    if entry is None:
        # Unknown and cross-tenant tokens look identical - no enumeration.
        abort(404, message="We couldn't find that place in the queue.")
    assert entry is not None
    return entry


@public_queue_blp.route("/<slug>/queue/status")
class PublicQueueStatus(MethodView):
    """Polled by the customer's status page - a POST only so the token stays
    out of the URL; it changes nothing except running the lazy sweep."""

    @limiter.limit(
        lambda: current_app.config["PUBLIC_QUEUE_STATUS_RATELIMIT"],
        key_func=_per_garage_client_key,
    )
    @public_queue_blp.arguments(QueueTokenSchema)
    @public_queue_blp.response(200, PublicQueueStatusSchema)
    def post(self, data, slug):
        garage = _get_garage_by_slug(slug)
        service.sweep_queue(garage)
        entry = _entry_by_token(garage, data["token"])
        settings = service.get_queue_settings(garage.id)
        return _status_payload(garage, entry, service.queue_snapshot(garage), settings)


@public_queue_blp.route("/<slug>/queue/cancel")
class PublicQueueCancel(MethodView):
    @limiter.limit(
        lambda: current_app.config["PUBLIC_QUEUE_JOIN_RATELIMIT"],
        key_func=_per_garage_client_key,
    )
    @public_queue_blp.arguments(QueueTokenSchema)
    @public_queue_blp.response(200, PublicQueueStatusSchema)
    def post(self, data, slug):
        garage = _get_garage_by_slug(slug)
        service.lock_garage(garage.id)
        entry = _entry_by_token(garage, data["token"])
        db.session.refresh(entry, with_for_update=True)
        service.cancel_entry(garage, entry, by_customer=True)
        settings = service.get_queue_settings(garage.id)
        return _status_payload(garage, entry, service.queue_snapshot(garage), settings)


# ---------------------------------------------------------------------------
# Staff routes
# ---------------------------------------------------------------------------


@queue_blp.route("")
class QueueDashboard(MethodView):
    @jwt_required()
    @queue_blp.doc(**_AUTH_DOC)
    @queue_blp.response(200, QueueDashboardSchema)
    def get(self):
        garage = _staff_garage()
        service.sweep_queue(garage)
        payload = _dashboard_payload(garage)
        db.session.commit()
        return payload


def _set_open(is_open: bool) -> dict:
    garage = _staff_garage()
    service.lock_garage(garage.id)
    service.get_queue_settings(garage.id).is_open = is_open
    db.session.commit()
    return _dashboard_payload(garage)


@queue_blp.route("/open")
class QueueOpen(MethodView):
    @jwt_required()
    @queue_blp.doc(**_AUTH_DOC)
    @queue_blp.response(200, QueueDashboardSchema)
    def post(self):
        return _set_open(True)


@queue_blp.route("/close")
class QueueClose(MethodView):
    """Stops new joins. Everyone already in the line stays and is served."""

    @jwt_required()
    @queue_blp.doc(**_AUTH_DOC)
    @queue_blp.response(200, QueueDashboardSchema)
    def post(self):
        return _set_open(False)


def _entry_response(garage: Garage, entry: QueueEntry) -> dict:
    return _entry_payload(
        entry, service.queue_snapshot(garage), service.get_queue_settings(garage.id)
    )


@queue_blp.route("/call-next")
class QueueCallNext(MethodView):
    @jwt_required()
    @queue_blp.doc(**_AUTH_DOC)
    @queue_blp.response(200, QueueEntrySchema)
    def post(self):
        garage = _staff_garage()
        return _entry_response(garage, service.call_entry(garage, None))


@queue_blp.route("/entries/<uuid:entry_id>/call")
class QueueEntryCall(MethodView):
    @jwt_required()
    @queue_blp.doc(**_AUTH_DOC)
    @queue_blp.response(200, QueueEntrySchema)
    def post(self, entry_id):
        garage = _staff_garage()
        return _entry_response(garage, service.call_entry(garage, entry_id))


@queue_blp.route("/entries/<uuid:entry_id>/start")
class QueueEntryStart(MethodView):
    """Customer has arrived - promotes the entry to an IN_PROGRESS appointment."""

    @jwt_required()
    @queue_blp.doc(**_AUTH_DOC)
    @queue_blp.arguments(StartServiceSchema)
    @queue_blp.response(200, QueueEntrySchema)
    def post(self, data, entry_id):
        employee = get_current_employee()
        assert employee is not None
        entry = service.start_service(employee, entry_id, data)
        return _entry_response(employee.garage, entry)


@queue_blp.route("/entries/<uuid:entry_id>/complete")
class QueueEntryComplete(MethodView):
    @jwt_required()
    @queue_blp.doc(**_AUTH_DOC)
    @queue_blp.response(200, QueueEntrySchema)
    def post(self, entry_id):
        garage = _staff_garage()
        return _entry_response(garage, service.complete_service(garage, entry_id))


@queue_blp.route("/entries/<uuid:entry_id>/no-show")
class QueueEntryNoShow(MethodView):
    @jwt_required()
    @queue_blp.doc(**_AUTH_DOC)
    @queue_blp.response(200, QueueEntrySchema)
    def post(self, entry_id):
        garage = _staff_garage()
        return _entry_response(garage, service.mark_no_show(garage, entry_id))


@queue_blp.route("/entries/<uuid:entry_id>/cancel")
class QueueEntryCancel(MethodView):
    @jwt_required()
    @queue_blp.doc(**_AUTH_DOC)
    @queue_blp.response(200, QueueEntrySchema)
    def post(self, entry_id):
        garage = _staff_garage()
        service.lock_garage(garage.id)
        entry = service.get_entry_for_update(garage.id, entry_id)
        service.cancel_entry(garage, entry, by_customer=False)
        return _entry_response(garage, entry)


@queue_blp.route("/order")
class QueueOrder(MethodView):
    @jwt_required()
    @queue_blp.doc(**_AUTH_DOC)
    @queue_blp.arguments(QueueReorderSchema)
    @queue_blp.response(200, QueueDashboardSchema)
    def put(self, data):
        garage = _staff_garage()
        service.reorder_waiting(garage, data["entry_ids"])
        return _dashboard_payload(garage)


@queue_blp.route("/settings")
class QueueSettingsResource(MethodView):
    @jwt_required()
    @queue_blp.doc(**_AUTH_DOC)
    @queue_blp.response(200, QueueSettingsSchema)
    def get(self):
        garage = _staff_garage()
        payload = _settings_payload(garage)
        db.session.commit()
        return payload

    @jwt_required()
    @owner_required
    @queue_blp.doc(**_AUTH_DOC)
    @queue_blp.arguments(QueueSettingsSchema)
    @queue_blp.response(200, QueueSettingsSchema)
    def put(self, data):
        garage = _staff_garage()
        settings = service.get_queue_settings(garage.id)
        type_id = data.get("default_appointment_type_id")
        if type_id is not None and (
            GarageAppointmentType.query.filter_by(id=type_id, garage_id=garage.id).first() is None
        ):
            abort(422, message="default_appointment_type_id is not a service of this business.")
        for key, value in data.items():
            setattr(settings, key, value)
        if settings.average_mode == AVERAGE_MODE_MANUAL and not settings.manual_average_minutes:
            db.session.rollback()
            abort(
                422,
                message="Set a manual average time to use manual mode.",
                errors={"json": {"manual_average_minutes": ["Required for MANUAL mode."]}},
            )
        db.session.commit()
        return _settings_payload(garage)


@queue_blp.route("/reserved-windows")
class ReservedWindowList(MethodView):
    @jwt_required()
    @queue_blp.doc(**_AUTH_DOC)
    @queue_blp.response(200, ReservedWindowSchema(many=True))
    def get(self):
        garage = _staff_garage()
        return (
            WalkInReservedWindow.query.filter_by(garage_id=garage.id)
            .order_by(
                WalkInReservedWindow.date.nulls_first(),
                WalkInReservedWindow.weekday,
                WalkInReservedWindow.starts_at,
            )
            .all()
        )

    @jwt_required()
    @owner_required
    @queue_blp.doc(**_AUTH_DOC)
    @queue_blp.arguments(ReservedWindowSchema)
    @queue_blp.response(201, ReservedWindowSchema)
    def post(self, data):
        garage = _staff_garage()
        window = WalkInReservedWindow(garage_id=garage.id, **data)
        db.session.add(window)
        db.session.commit()
        return window


@queue_blp.route("/reserved-windows/<uuid:window_id>")
class ReservedWindowResource(MethodView):
    @jwt_required()
    @owner_required
    @queue_blp.doc(**_AUTH_DOC)
    @queue_blp.response(204)
    def delete(self, window_id):
        garage = _staff_garage()
        window = WalkInReservedWindow.query.filter_by(id=window_id, garage_id=garage.id).first()
        if window is None:
            abort(404, message="Reserved window not found.")
        db.session.delete(window)
        db.session.commit()
