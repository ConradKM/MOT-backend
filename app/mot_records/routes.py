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


def _sync_vehicle_mot_expiry(vehicle):
    latest_expiry = (
        db.session.query(db.func.max(MOTRecord.expiry_date))
        .filter_by(vehicle_id=vehicle.id)
        .scalar()
    )
    vehicle.mot_expiry_date = latest_expiry


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

        record = MOTRecord(
            garage_id=garage_id,
            vehicle_id=vehicle.id,
            mot_date=data["mot_date"],
            expiry_date=data["expiry_date"],
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

        for field, value in data.items():
            setattr(record, field, value)

        _sync_vehicle_mot_expiry(vehicle)

        db.session.commit()

        return record
