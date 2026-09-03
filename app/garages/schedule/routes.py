"""Owner-facing CRUD for a garage's scheduling rules (Settings > Availability).

Same shape as app/appointments/statuses/routes.py: reads are open to any
authenticated employee, writes are OWNER-only, everything is scoped to the
caller's garage.
"""

from flask.views import MethodView
from flask_jwt_extended import jwt_required
from flask_smorest import Blueprint, abort
from sqlalchemy.exc import IntegrityError

from app.auth.decorators import owner_required
from app.auth.utils import get_current_employee
from app.extensions import db
from app.garages.schedule.defaults import seed_default_schedule
from app.models.garage_schedule import (
    GarageOpeningHours,
    GarageScheduleException,
    GarageScheduleSettings,
)

from .schemas import (
    GarageScheduleSchema,
    OpeningHoursReplaceSchema,
    ScheduleExceptionSchema,
    ScheduleSettingsSchema,
)

garage_schedule_blp = Blueprint(
    "garage-schedule",
    "garage-schedule",
    url_prefix="/api/garage/schedule",
    description="Per-garage opening hours, slot rules and one-off date "
    "exceptions that drive the public availability calendar.",
)

_AUTH_DOC = {"security": [{"bearerAuth": []}]}


def _garage_id():
    return get_current_employee().garage_id


def _ensure_seeded(garage_id):
    """Lazily create the default rows for garages that predate the seed."""
    row = GarageScheduleSettings.query.filter_by(garage_id=garage_id).first()
    if row is None:
        seed_default_schedule(garage_id, db.session)
        db.session.commit()
        row = GarageScheduleSettings.query.filter_by(garage_id=garage_id).first()
    return row


def _schedule_payload(garage_id):
    settings = _ensure_seeded(garage_id)
    return {
        "settings": settings,
        "opening_hours": (
            GarageOpeningHours.query.filter_by(garage_id=garage_id)
            .order_by(GarageOpeningHours.weekday)
            .all()
        ),
        "exceptions": (
            GarageScheduleException.query.filter_by(garage_id=garage_id)
            .order_by(GarageScheduleException.date)
            .all()
        ),
    }


@garage_schedule_blp.route("")
class GarageScheduleResource(MethodView):

    @jwt_required()
    @garage_schedule_blp.doc(**_AUTH_DOC)
    @garage_schedule_blp.response(200, GarageScheduleSchema)
    def get(self):
        return _schedule_payload(_garage_id())


@garage_schedule_blp.route("/settings")
class GarageScheduleSettingsResource(MethodView):

    @jwt_required()
    @owner_required
    @garage_schedule_blp.doc(**_AUTH_DOC)
    @garage_schedule_blp.arguments(ScheduleSettingsSchema)
    @garage_schedule_blp.response(200, ScheduleSettingsSchema)
    def put(self, data):
        garage_id = _garage_id()
        settings = _ensure_seeded(garage_id)
        for field, value in data.items():
            setattr(settings, field, value)
        db.session.commit()
        return settings


@garage_schedule_blp.route("/opening-hours")
class GarageOpeningHoursResource(MethodView):

    @jwt_required()
    @owner_required
    @garage_schedule_blp.doc(**_AUTH_DOC)
    @garage_schedule_blp.arguments(OpeningHoursReplaceSchema)
    @garage_schedule_blp.response(200, GarageScheduleSchema)
    def put(self, data):
        garage_id = _garage_id()
        _ensure_seeded(garage_id)

        by_weekday = {
            oh.weekday: oh
            for oh in GarageOpeningHours.query.filter_by(garage_id=garage_id).all()
        }
        for entry in data["opening_hours"]:
            if entry["opens_at"] >= entry["closes_at"] and not entry["is_closed"]:
                abort(
                    422,
                    message=f"weekday {entry['weekday']}: opens_at must be "
                    "before closes_at.",
                )
            row = by_weekday.get(entry["weekday"])
            if row is None:
                row = GarageOpeningHours(garage_id=garage_id, weekday=entry["weekday"])
                db.session.add(row)
            row.opens_at = entry["opens_at"]
            row.closes_at = entry["closes_at"]
            row.is_closed = entry["is_closed"]

        db.session.commit()
        return _schedule_payload(garage_id)


@garage_schedule_blp.route("/exceptions")
class GarageScheduleExceptionList(MethodView):

    @jwt_required()
    @owner_required
    @garage_schedule_blp.doc(**_AUTH_DOC)
    @garage_schedule_blp.arguments(ScheduleExceptionSchema)
    @garage_schedule_blp.response(201, ScheduleExceptionSchema)
    def post(self, data):
        garage_id = _garage_id()
        _ensure_seeded(garage_id)

        if not data["is_closed"] and (
            data["opens_at"] is None or data["closes_at"] is None
        ):
            abort(
                422,
                message="A non-closed exception needs both opens_at and closes_at.",
            )

        exc = GarageScheduleException(
            garage_id=garage_id,
            date=data["date"],
            is_closed=data["is_closed"],
            opens_at=data["opens_at"],
            closes_at=data["closes_at"],
            note=data["note"],
        )
        db.session.add(exc)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            abort(409, message="An exception already exists for that date.")
        return exc


@garage_schedule_blp.route("/exceptions/<uuid:exception_id>")
class GarageScheduleExceptionResource(MethodView):

    @jwt_required()
    @owner_required
    @garage_schedule_blp.doc(**_AUTH_DOC)
    @garage_schedule_blp.response(204)
    def delete(self, exception_id):
        garage_id = _garage_id()
        exc = GarageScheduleException.query.filter_by(
            id=exception_id, garage_id=garage_id
        ).first()
        if exc is None:
            abort(404, message="Schedule exception not found")
        db.session.delete(exc)
        db.session.commit()
        return ""
