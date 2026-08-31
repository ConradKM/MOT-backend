from flask.views import MethodView
from flask_jwt_extended import jwt_required
from flask_smorest import Blueprint, abort
from sqlalchemy.exc import IntegrityError

from app.auth.decorators import owner_required
from app.auth.utils import get_current_employee
from app.extensions import db
from app.models.appointments.appointment_type import GarageAppointmentType

from .schemas import (
    AppointmentTypeQueryArgsSchema,
    AppointmentTypeSchema,
    AppointmentTypeUpdateSchema,
)

appointment_types_blp = Blueprint(
    "appointment-types",
    "appointment-types",
    url_prefix="/api/appointment-types",
    description="Garage-defined appointment types (replaces the old fixed enum)",
)


def get_owned_appointment_type(appointment_type_id, garage_id):
    appointment_type = GarageAppointmentType.query.filter_by(
        id=appointment_type_id, garage_id=garage_id
    ).first()

    if not appointment_type:
        abort(404, message="Appointment type not found")

    return appointment_type


@appointment_types_blp.route("/")
class AppointmentTypeList(MethodView):

    @jwt_required()
    @appointment_types_blp.arguments(AppointmentTypeQueryArgsSchema, location="query")
    @appointment_types_blp.response(200, AppointmentTypeSchema(many=True))
    def get(self, args):
        garage_id = get_current_employee().garage_id

        query = GarageAppointmentType.query.filter_by(garage_id=garage_id)

        # No filter by default - the owner's own settings view needs to see
        # hidden/deprecated types too, to manage or re-enable them. Pass
        # ?status=ACTIVE to get only what should be offered for new bookings.
        if args.get("status") is not None:
            query = query.filter(GarageAppointmentType.status == args["status"])

        return query.order_by(GarageAppointmentType.name).all()

    @jwt_required()
    @owner_required
    @appointment_types_blp.arguments(AppointmentTypeSchema)
    @appointment_types_blp.response(201, AppointmentTypeSchema)
    def post(self, data):
        garage_id = get_current_employee().garage_id

        appointment_type = GarageAppointmentType(
            garage_id=garage_id,
            name=data["name"],
            description=data.get("description"),
            base_price=data.get("base_price"),
            status=data.get("status") or "ACTIVE",
        )

        db.session.add(appointment_type)
        db.session.commit()

        return appointment_type


@appointment_types_blp.route("/<uuid:appointment_type_id>")
class AppointmentTypeResource(MethodView):

    @jwt_required()
    @appointment_types_blp.response(200, AppointmentTypeSchema)
    def get(self, appointment_type_id):
        garage_id = get_current_employee().garage_id

        return get_owned_appointment_type(appointment_type_id, garage_id)

    @jwt_required()
    @owner_required
    @appointment_types_blp.arguments(AppointmentTypeUpdateSchema)
    @appointment_types_blp.response(200, AppointmentTypeSchema)
    def patch(self, data, appointment_type_id):
        garage_id = get_current_employee().garage_id
        appointment_type = get_owned_appointment_type(appointment_type_id, garage_id)

        for field, value in data.items():
            setattr(appointment_type, field, value)

        db.session.commit()

        return appointment_type

    @jwt_required()
    @owner_required
    @appointment_types_blp.response(204)
    def delete(self, appointment_type_id):
        garage_id = get_current_employee().garage_id
        appointment_type = get_owned_appointment_type(appointment_type_id, garage_id)

        db.session.delete(appointment_type)

        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            abort(
                409,
                message="Cannot delete an appointment type that has appointments booked against it.",
            )

        return ""
