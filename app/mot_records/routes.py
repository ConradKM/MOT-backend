from flask.views import MethodView
from flask_jwt_extended import jwt_required
from flask_smorest import Blueprint, abort

from app.auth.utils import get_current_employee
from app.extensions import db
from app.models.mot_record import MOTRecord
from app.models.vehicle import Vehicle

from .schemas import MOTRecordSchema, MOTRecordUpdateSchema

mot_records_blp = Blueprint(
    "mot-records",
    "mot-records",
    url_prefix="/api/vehicles/<uuid:vehicle_id>/mot-records",
    description="MOT test history for a vehicle",
)


def _get_owned_vehicle(vehicle_id, garage_id):
    vehicle = Vehicle.query.filter_by(id=vehicle_id, garage_id=garage_id).first()

    if not vehicle:
        abort(404, message="Vehicle not found")

    return vehicle


def _resolve_expiry_date(mot_date, expiry_date, result):
    """The expiry_date a record should actually be stored with.

    A FAILED test never grants a new valid period - DVSA doesn't issue an
    expiry for a fail, so this defaults it to `mot_date` itself (zero forward
    validity) when the caller didn't supply one, and never requires it to be
    in the future. A PASS must have a real expiry strictly after the test
    date; historically this is where "PASS records with expiry == mot_date"
    (an obviously wrong annual certificate) slipped through unvalidated.
    """
    if result == "FAIL":
        return expiry_date if expiry_date is not None else mot_date

    if expiry_date is None:
        abort(422, message="expiry_date is required for a PASS result.")
    if expiry_date <= mot_date:
        abort(
            422,
            message="expiry_date must be after mot_date for a PASS result.",
        )
    return expiry_date


def _sync_vehicle_mot_expiry(vehicle):
    """The vehicle's current MOT expiry is the latest expiry among its PASS
    records only. A FAIL is never allowed to extend/replace it - taking
    MAX(expiry_date) across every result let a fail's placeholder date
    (wrongly) look like a fresh annual certificate."""
    latest_pass_expiry = (
        db.session.query(db.func.max(MOTRecord.expiry_date))
        .filter_by(vehicle_id=vehicle.id, result="PASS")
        .scalar()
    )
    vehicle.mot_expiry_date = latest_pass_expiry


@mot_records_blp.route("/")
class MOTRecordList(MethodView):

    @jwt_required()
    @mot_records_blp.response(200, MOTRecordSchema(many=True))
    def get(self, vehicle_id):
        garage_id = get_current_employee().garage_id
        vehicle = _get_owned_vehicle(vehicle_id, garage_id)

        return (
            MOTRecord.query.filter_by(vehicle_id=vehicle.id)
            .order_by(MOTRecord.mot_date.desc())
            .all()
        )

    @jwt_required()
    @mot_records_blp.arguments(MOTRecordSchema)
    @mot_records_blp.response(201, MOTRecordSchema)
    def post(self, data, vehicle_id):
        garage_id = get_current_employee().garage_id
        vehicle = _get_owned_vehicle(vehicle_id, garage_id)

        expiry_date = _resolve_expiry_date(
            data["mot_date"], data.get("expiry_date"), data["result"]
        )

        record = MOTRecord(
            garage_id=garage_id,
            vehicle_id=vehicle.id,
            mot_date=data["mot_date"],
            expiry_date=expiry_date,
            result=data["result"],
            notes=data.get("notes"),
        )

        db.session.add(record)
        db.session.flush()

        _sync_vehicle_mot_expiry(vehicle)

        db.session.commit()

        return record


@mot_records_blp.route("/<uuid:record_id>")
class MOTRecordResource(MethodView):

    @jwt_required()
    @mot_records_blp.response(200, MOTRecordSchema)
    def get(self, vehicle_id, record_id):
        garage_id = get_current_employee().garage_id
        vehicle = _get_owned_vehicle(vehicle_id, garage_id)

        record = MOTRecord.query.filter_by(id=record_id, vehicle_id=vehicle.id).first()

        if not record:
            abort(404, message="MOT record not found")

        return record

    @jwt_required()
    @mot_records_blp.arguments(MOTRecordUpdateSchema)
    @mot_records_blp.response(200, MOTRecordSchema)
    def patch(self, data, vehicle_id, record_id):
        garage_id = get_current_employee().garage_id
        vehicle = _get_owned_vehicle(vehicle_id, garage_id)

        record = MOTRecord.query.filter_by(id=record_id, vehicle_id=vehicle.id).first()

        if not record:
            abort(404, message="MOT record not found")

        if {"mot_date", "expiry_date", "result"} & data.keys():
            mot_date = data.get("mot_date", record.mot_date)
            result = data.get("result", record.result)
            expiry_date = data.get("expiry_date", record.expiry_date)
            record.mot_date = mot_date
            record.result = result
            record.expiry_date = _resolve_expiry_date(mot_date, expiry_date, result)

        if "notes" in data:
            record.notes = data["notes"]

        _sync_vehicle_mot_expiry(vehicle)

        db.session.commit()

        return record
