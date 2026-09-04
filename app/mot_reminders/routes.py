"""Staff MOT reminders: the visibility page, the manual "Send reminder"
action, and the owner-configurable reminder schedule.

Everything is scoped to the authenticated employee's garage. Reads are open to
any employee; changing the schedule is OWNER-only (same pattern as
Settings > Availability).
"""

from collections import defaultdict
from datetime import UTC, datetime

from flask.views import MethodView
from flask_jwt_extended import jwt_required
from flask_smorest import Blueprint, abort

from app.auth.decorators import owner_required
from app.auth.utils import get_current_employee
from app.extensions import db
from app.models.customer import Customer
from app.models.mot_reminder_settings import MOTReminderSettings
from app.models.reminder import Reminder
from app.models.vehicle import Vehicle

from .defaults import resolve_mot_reminder_settings, seed_mot_reminder_settings
from .schemas import (
    ManualReminderResultSchema,
    ManualReminderSendSchema,
    MOTReminderRowSchema,
    MOTReminderSettingsSchema,
)
from .service import (
    attach_initiator_names,
    compute_reminder_state,
    mot_booking_active_for,
    send_manual_reminder,
)

mot_reminders_blp = Blueprint(
    "mot-reminders",
    "mot-reminders",
    url_prefix="/api/mot-reminders",
    description="Staff visibility of vehicle MOT expiries, manual reminders "
    "and the per-garage reminder schedule.",
)

_AUTH_DOC = {"security": [{"bearerAuth": []}]}


def _garage_id():
    return get_current_employee().garage_id


def _ensure_settings(garage_id) -> MOTReminderSettings:
    row = MOTReminderSettings.query.filter_by(garage_id=garage_id).first()
    if row is None:
        seed_mot_reminder_settings(garage_id, db.session)
        db.session.commit()
        row = MOTReminderSettings.query.filter_by(garage_id=garage_id).first()
    return row


def _normalise_stage_order(row: MOTReminderSettings) -> None:
    """Reassign stage1/2/3 so days_before runs furthest-out -> closest, no
    matter what order the caller sent them in."""
    trio = sorted(
        [
            (row.stage1_enabled, row.stage1_days_before),
            (row.stage2_enabled, row.stage2_days_before),
            (row.stage3_enabled, row.stage3_days_before),
        ],
        key=lambda pair: pair[1],
        reverse=True,
    )
    (row.stage1_enabled, row.stage1_days_before) = trio[0]
    (row.stage2_enabled, row.stage2_days_before) = trio[1]
    (row.stage3_enabled, row.stage3_days_before) = trio[2]


def _reject_duplicate_intervals(row: MOTReminderSettings) -> None:
    enabled_days = [
        days
        for enabled, days in (
            (row.stage1_enabled, row.stage1_days_before),
            (row.stage2_enabled, row.stage2_days_before),
            (row.stage3_enabled, row.stage3_days_before),
        )
        if enabled
    ]
    if len(enabled_days) != len(set(enabled_days)):
        abort(
            422,
            message="Enabled reminder stages must use different intervals.",
        )


@mot_reminders_blp.route("/")
class MOTReminderList(MethodView):

    @jwt_required()
    @mot_reminders_blp.doc(**_AUTH_DOC)
    @mot_reminders_blp.response(200, MOTReminderRowSchema(many=True))
    def get(self):
        garage_id = _garage_id()
        today = datetime.now(UTC).date()

        settings = resolve_mot_reminder_settings(garage_id, db.session)

        vehicles = (
            Vehicle.query.filter(
                Vehicle.garage_id == garage_id,
                Vehicle.mot_expiry_date.isnot(None),
            )
            .order_by(Vehicle.mot_expiry_date)
            .all()
        )
        customers = {
            c.id: c for c in Customer.query.filter_by(garage_id=garage_id).all()
        }

        events_by_vehicle = defaultdict(list)
        all_events = Reminder.query.filter(
            Reminder.garage_id == garage_id, Reminder.type.ilike("MOT%")
        ).all()
        attach_initiator_names(all_events)
        for r in all_events:
            events_by_vehicle[r.vehicle_id].append(r)

        rows = []
        for v in vehicles:
            customer = customers.get(v.customer_id)
            booking_active = mot_booking_active_for(
                v.id, garage_id, v.mot_expiry_date, db.session
            )
            state = compute_reminder_state(
                vehicle=v,
                settings=settings,
                events=events_by_vehicle.get(v.id, []),
                booking_active=booking_active,
                today=today,
            )
            rows.append(
                {
                    "vehicle_id": v.id,
                    "customer_id": v.customer_id,
                    "customer_name": (
                        f"{customer.first_name} {customer.last_name}"
                        if customer
                        else "—"
                    ),
                    "customer_email": customer.email if customer else None,
                    "registration_number": v.registration_number,
                    "make": v.make,
                    "model": v.model,
                    "mot_expiry_date": v.mot_expiry_date,
                    "can_send_manual": customer is not None,
                    **state,
                }
            )
        return rows


@mot_reminders_blp.route("/<uuid:vehicle_id>/send")
class MOTReminderManualSend(MethodView):

    @jwt_required()
    @mot_reminders_blp.doc(**_AUTH_DOC)
    @mot_reminders_blp.arguments(ManualReminderSendSchema)
    @mot_reminders_blp.response(201, ManualReminderResultSchema)
    def post(self, data, vehicle_id):
        garage_id = _garage_id()
        employee = get_current_employee()

        vehicle = Vehicle.query.filter_by(
            id=vehicle_id, garage_id=garage_id
        ).first()
        if vehicle is None:
            abort(404, message="Vehicle not found")

        customer = db.session.get(Customer, vehicle.customer_id)
        if customer is None or customer.garage_id != garage_id:
            abort(404, message="Vehicle has no customer on this garage.")

        if not data["acknowledge_booking"] and mot_booking_active_for(
            vehicle.id, garage_id, vehicle.mot_expiry_date, db.session
        ):
            abort(
                409,
                message="This vehicle already has an MOT booking. Confirm to "
                "send a reminder anyway.",
            )

        reminder = send_manual_reminder(
            vehicle=vehicle,
            garage=vehicle.garage,
            initiated_by_id=employee.id,
            channel=data.get("channel"),
            session=db.session,
        )
        db.session.commit()
        return reminder


@mot_reminders_blp.route("/settings")
class MOTReminderSettingsResource(MethodView):

    @jwt_required()
    @mot_reminders_blp.doc(**_AUTH_DOC)
    @mot_reminders_blp.response(200, MOTReminderSettingsSchema)
    def get(self):
        return resolve_mot_reminder_settings(_garage_id(), db.session)

    @jwt_required()
    @owner_required
    @mot_reminders_blp.doc(**_AUTH_DOC)
    @mot_reminders_blp.arguments(MOTReminderSettingsSchema)
    @mot_reminders_blp.response(200, MOTReminderSettingsSchema)
    def put(self, data):
        garage_id = _garage_id()
        row = _ensure_settings(garage_id)

        for field, value in data.items():
            setattr(row, field, value)

        _normalise_stage_order(row)
        _reject_duplicate_intervals(row)

        db.session.commit()
        return row
