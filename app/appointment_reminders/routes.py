"""Generic "Reminders" settings surface.

``/api/mot-reminders/*`` keeps working unchanged (existing MOT reminder
functionality is preserved). This blueprint adds:

* ``GET /api/reminders/settings`` - an aggregate read of every reminder
  type's configuration, for a single owner-facing "Reminders" settings page.
* ``GET/PUT /api/reminders/appointment-settings`` - read/update the
  appointment-reminder configuration (enabled, channels, timings).

Reads are open to any employee; writes are OWNER-only, same convention as
``/api/mot-reminders/settings``.
"""

from flask.views import MethodView
from flask_jwt_extended import jwt_required
from flask_smorest import Blueprint

from app.auth.decorators import owner_required
from app.auth.utils import get_current_employee
from app.extensions import db
from app.models.appointment_reminder_settings import (
    AppointmentReminderSettings,
    AppointmentReminderTiming,
)
from app.mot_reminders.defaults import resolve_mot_reminder_settings

from .defaults import resolve_appointment_reminder_settings, seed_appointment_reminder_settings
from .schemas import AppointmentReminderSettingsSchema, RemindersSettingsSchema
from .service import available_channels

reminders_blp = Blueprint(
    "reminders",
    "reminders",
    url_prefix="/api/reminders",
    description="Owner-facing reminder settings across all reminder types "
    "(MOT reminders, appointment reminders, ...).",
)

_AUTH_DOC: dict[str, list[dict[str, list[str]]]] = {"security": [{"bearerAuth": []}]}


def _garage_id():
    return get_current_employee().garage_id


def _with_available_channels(settings, garage):
    settings.available_channels = available_channels(garage)
    return settings


def _ensure_settings(garage_id) -> AppointmentReminderSettings:
    row = AppointmentReminderSettings.query.filter_by(garage_id=garage_id).first()
    if row is None:
        seed_appointment_reminder_settings(garage_id, db.session)
        db.session.commit()
        row = AppointmentReminderSettings.query.filter_by(garage_id=garage_id).first()
    assert row is not None, "seed_appointment_reminder_settings must create exactly one row"
    return row  # type: ignore[no-any-return]


@reminders_blp.route("/settings")
class RemindersSettingsResource(MethodView):
    """Read-only aggregate: what every reminder type is currently configured
    to do, for the "Reminders" settings page overview."""

    @jwt_required()
    @reminders_blp.doc(**_AUTH_DOC)
    @reminders_blp.response(200, RemindersSettingsSchema)
    def get(self):
        employee = get_current_employee()
        garage_id = employee.garage_id
        appointment_settings = _with_available_channels(
            resolve_appointment_reminder_settings(garage_id, db.session), employee.garage
        )
        return {
            "mot": resolve_mot_reminder_settings(garage_id, db.session),
            "appointment": appointment_settings,
        }


@reminders_blp.route("/appointment-settings")
class AppointmentReminderSettingsResource(MethodView):
    @jwt_required()
    @reminders_blp.doc(**_AUTH_DOC)
    @reminders_blp.response(200, AppointmentReminderSettingsSchema)
    def get(self):
        employee = get_current_employee()
        return _with_available_channels(
            resolve_appointment_reminder_settings(employee.garage_id, db.session), employee.garage
        )

    @jwt_required()
    @owner_required
    @reminders_blp.doc(**_AUTH_DOC)
    @reminders_blp.arguments(AppointmentReminderSettingsSchema)
    @reminders_blp.response(200, AppointmentReminderSettingsSchema)
    def put(self, data):
        employee = get_current_employee()
        garage_id = employee.garage_id
        row = _ensure_settings(garage_id)

        row.enabled = data["enabled"]
        row.channels = data["channels"]

        # Full replace of timings - simplest semantics for "here are the lead
        # times I want" (see schema docstring).
        AppointmentReminderTiming.query.filter_by(settings_id=row.id).delete()
        db.session.flush()
        for timing in data["timings"]:
            db.session.add(
                AppointmentReminderTiming(
                    settings_id=row.id,
                    hours_before=timing["hours_before"],
                    enabled=timing.get("enabled", True),
                )
            )

        db.session.commit()
        db.session.refresh(row)
        return _with_available_channels(row, employee.garage)
