import hashlib
import logging
from datetime import timedelta

from flask import Response, current_app, request
from flask.views import MethodView
from flask_jwt_extended import jwt_required
from flask_smorest import Blueprint, abort

from app.auth.utils import get_current_employee
from app.extensions import db, limiter
from app.models.appointments.appointment_type import GarageAppointmentType
from app.models.calendar_feed import CalendarFeed
from app.models.employee import Employee
from app.models.garage import Garage

from .feeds import feed_urls, find_active_feed, hash_token, issue_token
from .ics import build_calendar
from .schemas import (
    CalendarExportQueryArgsSchema,
    CalendarFeedCreateSchema,
    CalendarFeedSchema,
    CalendarFeedWithUrlSchema,
)
from .service import (
    CalendarExportError,
    active_feed_count,
    appointments_for_export,
    feed_appointment_type_ids,
    feed_date_range,
    feed_employee_ids,
    is_owner,
    resolve_appointment_type_ids,
    resolve_employee_ids,
    shows_worker_names,
    utcnow,
    validate_date_range,
)

logger = logging.getLogger(__name__)

calendar_export_blp = Blueprint(
    "calendar_export",
    "calendar_export",
    url_prefix="/api/calendar-export",
    description="Export appointments to Apple / Google Calendar: a one-off .ics download, "
    "and private read-only subscription feeds. Events carry the service, vehicle "
    "registration, time, garage address and a link back to the app - never customer details.",
)

public_calendar_feeds_blp = Blueprint(
    "public_calendar_feeds",
    "public_calendar_feeds",
    url_prefix="/api/calendar-feeds",
    description="Calendar subscription feeds, fetched by calendar apps. Unauthenticated: "
    "the unguessable token in the path is the credential.",
)

_AUTH_DOC: dict[str, list[dict[str, list[str]]]] = {"security": [{"bearerAuth": []}]}
_ICS_RESPONSE_DOC = {
    "200": {
        "description": "An iCalendar (RFC 5545) file.",
        "content": {"text/calendar": {"schema": {"type": "string"}}},
    }
}
_ICS_MIMETYPE = "text/calendar"
# Long enough that a client re-polling hourly reuses its copy via the ETag,
# short enough that a moved appointment shows up promptly.
_FEED_CACHE_CONTROL = "private, max-age=300"
# Writing last_accessed_at on every poll would be a row update per subscriber
# per hour for nothing; this resolution is plenty for "is this link in use?".
_ACCESS_STAMP_RESOLUTION = timedelta(minutes=5)

_FEED_NOT_FOUND = "Calendar feed not found."


def _ics_response(body: bytes) -> Response:
    return Response(body, mimetype=_ICS_MIMETYPE, content_type=f"{_ICS_MIMETYPE}; charset=utf-8")


def _require_employee():
    employee = get_current_employee()
    if employee is None:
        abort(403, message="Employee account required.")
    return employee


def _feed_payload(feed: CalendarFeed, raw_token: str | None = None) -> dict:
    payload = {
        "id": feed.id,
        "name": feed.name,
        "scope": feed.scope,
        "employee_ids": sorted((e.id for e in feed.employees), key=str),
        "appointment_type_ids": None
        if feed.all_appointment_types
        else sorted((t.id for t in feed.appointment_types), key=str),
        "days_back": feed.days_back,
        "days_forward": feed.days_forward,
        "created_by_employee_id": feed.created_by_employee_id,
        "created_at": feed.created_at,
        "last_accessed_at": feed.last_accessed_at,
    }
    if raw_token is not None:
        payload.update(feed_urls(raw_token))
    return payload


def _get_visible_feed(feed_id, employee: Employee):
    """A live feed in the caller's garage that they created, or - for an
    owner - anyone's. 404 otherwise, so other garages' ids reveal nothing."""
    feed = CalendarFeed.query.filter_by(
        id=feed_id, garage_id=employee.garage_id, revoked_at=None
    ).first()
    if feed is None or (feed.created_by_employee_id != employee.id and not is_owner(employee)):
        abort(404, message=_FEED_NOT_FOUND)
    return feed


@calendar_export_blp.route("/appointments.ics")
class CalendarExportDownload(MethodView):
    @jwt_required()
    @calendar_export_blp.doc(**_AUTH_DOC, responses=_ICS_RESPONSE_DOC)
    @calendar_export_blp.arguments(CalendarExportQueryArgsSchema, location="query")
    def get(self, args):
        """Download appointments as a one-off .ics file (a snapshot to import)."""
        employee = _require_employee()
        garage = db.session.get(Garage, employee.garage_id)

        try:
            employee_ids = resolve_employee_ids(employee, args["scope"], args["employee_ids"])
            type_ids = resolve_appointment_type_ids(garage.id, args["appointment_type_ids"])
            validate_date_range(args["start_date"], args["end_date"])
            appointments = appointments_for_export(
                garage, employee_ids, type_ids, args["start_date"], args["end_date"]
            )
        except CalendarExportError as err:
            abort(err.status_code, message=err.message)

        body = build_calendar(
            garage,
            appointments,
            show_worker=shows_worker_names(employee_ids),
            calendar_name=f"{garage.name} - Appointments",
            is_feed=False,
        )
        response = _ics_response(body)
        response.headers["Content-Disposition"] = 'attachment; filename="appointments.ics"'
        response.headers["Cache-Control"] = "no-store"
        return response


@calendar_export_blp.route("/feeds")
class CalendarFeedList(MethodView):
    @jwt_required()
    @calendar_export_blp.doc(**_AUTH_DOC)
    @calendar_export_blp.response(200, CalendarFeedSchema(many=True))
    def get(self):
        """Live subscription links: your own, or every one in the garage for an owner."""
        employee = _require_employee()
        query = CalendarFeed.query.filter_by(garage_id=employee.garage_id, revoked_at=None)
        if not is_owner(employee):
            query = query.filter_by(created_by_employee_id=employee.id)
        return [_feed_payload(feed) for feed in query.order_by(CalendarFeed.created_at).all()]

    @jwt_required()
    @calendar_export_blp.doc(**_AUTH_DOC)
    @calendar_export_blp.arguments(CalendarFeedCreateSchema)
    @calendar_export_blp.response(201, CalendarFeedWithUrlSchema)
    def post(self, data):
        """Create a subscription link. The URL is in this response only."""
        employee = _require_employee()

        try:
            employee_ids = resolve_employee_ids(employee, data["scope"], data["employee_ids"])
            type_ids = resolve_appointment_type_ids(
                employee.garage_id, data["appointment_type_ids"]
            )
        except CalendarExportError as err:
            abort(err.status_code, message=err.message)

        limit = current_app.config["CALENDAR_FEEDS_PER_EMPLOYEE"]
        if active_feed_count(employee) >= limit:
            abort(
                422,
                message=f"You already have {limit} calendar links. Revoke one you no longer "
                "use before creating another.",
            )

        feed = CalendarFeed(
            garage_id=employee.garage_id,
            created_by_employee_id=employee.id,
            name=data["name"].strip() or "Appointments",
            scope=data["scope"],
            all_appointment_types=type_ids is None,
            days_back=data["days_back"],
            days_forward=data["days_forward"],
        )
        if data["scope"] == "SELECTED" and employee_ids is not None:
            feed.employees = Employee.query.filter(Employee.id.in_(employee_ids)).all()
        if type_ids is not None:
            feed.appointment_types = GarageAppointmentType.query.filter(
                GarageAppointmentType.id.in_(type_ids)
            ).all()

        raw_token = issue_token(feed)
        db.session.add(feed)
        db.session.commit()
        return _feed_payload(feed, raw_token)


@calendar_export_blp.route("/feeds/<uuid:feed_id>")
class CalendarFeedDetail(MethodView):
    @jwt_required()
    @calendar_export_blp.doc(**_AUTH_DOC)
    @calendar_export_blp.response(204)
    def delete(self, feed_id):
        """Revoke a subscription link (the creator's own, or any in the garage for an
        owner). The URL stops working immediately."""
        feed = _get_visible_feed(feed_id, _require_employee())
        feed.revoked_at = utcnow()
        db.session.commit()


@calendar_export_blp.route("/feeds/<uuid:feed_id>/regenerate")
class CalendarFeedRegenerate(MethodView):
    @jwt_required()
    @calendar_export_blp.doc(**_AUTH_DOC)
    @calendar_export_blp.response(200, CalendarFeedWithUrlSchema)
    def post(self, feed_id):
        """Rotate a link's URL: the old one stops working immediately and the new
        one is in this response only. Only the link's creator can do this."""
        employee = _require_employee()
        feed = _get_visible_feed(feed_id, employee)
        if feed.created_by_employee_id != employee.id:
            abort(403, message="Only the person who created this link can regenerate it.")

        # A rotated URL must not carry authority the creator no longer has.
        try:
            feed_employee_ids(feed)
        except CalendarExportError:
            abort(403, message="You no longer have permission for this link's calendars.")

        raw_token = issue_token(feed)
        db.session.commit()
        return _feed_payload(feed, raw_token)


def _feed_rate_limit_key() -> str:
    # Per feed, not per IP: see CALENDAR_FEED_RATELIMIT in app/config.py. Keyed
    # by the token's hash so the raw token never reaches the limiter's store.
    token = (request.view_args or {}).get("token", "")
    return f"calendar-feed:{hash_token(token)}"


@public_calendar_feeds_blp.route("/<string:token>.ics")
class CalendarFeedFetch(MethodView):
    @limiter.limit(
        lambda: current_app.config["CALENDAR_FEED_RATELIMIT"], key_func=_feed_rate_limit_key
    )
    @public_calendar_feeds_blp.doc(responses=_ICS_RESPONSE_DOC)
    def get(self, token):
        """Fetch a subscription feed. Unknown, malformed, revoked and no-longer-
        authorised tokens all get the same 404."""
        feed = find_active_feed(token)
        if feed is None:
            abort(404, message=_FEED_NOT_FOUND)

        # The garage comes from the feed row and nothing else.
        garage = feed.garage
        now = utcnow()
        try:
            employee_ids = feed_employee_ids(feed)
            start_date, end_date = feed_date_range(garage, feed, now)
            appointments = appointments_for_export(
                garage, employee_ids, feed_appointment_type_ids(feed), start_date, end_date
            )
        except CalendarExportError as err:
            if err.status_code == 422:
                # Only a feed that outgrew the event cap gets here. Logged by
                # id - never by token or URL - so the owner can be told.
                logger.warning("[calendar_feed] feed_id=%s not served: %s", feed.id, err.message)
            abort(404, message=_FEED_NOT_FOUND)

        body = build_calendar(
            garage,
            appointments,
            show_worker=shows_worker_names(employee_ids),
            calendar_name=f"{garage.name} - {feed.name}",
            is_feed=True,
        )

        # After serialising: a commit expires every loaded row, and the events
        # above would otherwise lazy-load their relationships one by one.
        if feed.last_accessed_at is None or now - feed.last_accessed_at > _ACCESS_STAMP_RESOLUTION:
            feed.last_accessed_at = now
            db.session.commit()
        response = _ics_response(body)
        response.set_etag(hashlib.sha256(body).hexdigest())
        response.headers["Cache-Control"] = _FEED_CACHE_CONTROL
        # The URL is the credential: keep it out of referrers and search indexes.
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Robots-Tag"] = "noindex"
        return response.make_conditional(request)
