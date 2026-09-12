from flask.views import MethodView
from flask_jwt_extended import jwt_required
from flask_smorest import Blueprint, abort
from sqlalchemy.exc import IntegrityError

from app.auth.decorators import owner_required
from app.auth.utils import get_current_employee
from app.extensions import db
from app.models.appointments.appointment_type import GarageAppointmentType
from app.payments.money import DepositConfigError, validate_deposit_config

from .schemas import (
    AppointmentTypeQueryArgsSchema,
    AppointmentTypeSchema,
    AppointmentTypeUpdateSchema,
)


def _validate_deposit_or_abort(*, deposit_required, deposit_type, deposit_value, base_price):
    try:
        validate_deposit_config(
            deposit_required=deposit_required,
            deposit_type=deposit_type,
            deposit_value=deposit_value,
            base_price=base_price,
        )
    except DepositConfigError as exc:
        abort(422, message=str(exc), errors={"json": {"deposit_value": [str(exc)]}})


appointment_types_blp = Blueprint(
    "appointment-types",
    "appointment-types",
    url_prefix="/api/appointment-types",
    description="Business-defined appointment types (replaces the old fixed enum)",
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

        deposit_required = data.get("deposit_required", False)
        # Off means off: never persist a stray type/value alongside a
        # disabled deposit, regardless of what the client sent.
        deposit_type = data.get("deposit_type") if deposit_required else None
        deposit_value = data.get("deposit_value") if deposit_required else None

        _validate_deposit_or_abort(
            deposit_required=deposit_required,
            deposit_type=deposit_type,
            deposit_value=deposit_value,
            base_price=data.get("base_price"),
        )

        appointment_type = GarageAppointmentType(
            garage_id=garage_id,
            name=data["name"],
            description=data.get("description"),
            base_price=data.get("base_price"),
            default_duration_minutes=data.get("default_duration_minutes"),
            status=data.get("status") or "ACTIVE",
            deposit_required=deposit_required,
            deposit_type=deposit_type,
            deposit_value=deposit_value,
            deposit_currency=data.get("deposit_currency") or "GBP",
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

        # Merge onto the row's *current* values so a partial PATCH (e.g. just
        # {"deposit_required": true}) is validated against what the type
        # already has, not against an incomplete in-flight body.
        deposit_required = data.get("deposit_required", appointment_type.deposit_required)
        deposit_type = (
            data.get("deposit_type", appointment_type.deposit_type) if deposit_required else None
        )
        deposit_value = (
            data.get("deposit_value", appointment_type.deposit_value) if deposit_required else None
        )
        base_price = data.get("base_price", appointment_type.base_price)

        _validate_deposit_or_abort(
            deposit_required=deposit_required,
            deposit_type=deposit_type,
            deposit_value=deposit_value,
            base_price=base_price,
        )

        for field, value in data.items():
            if field in ("deposit_required", "deposit_type", "deposit_value"):
                continue
            setattr(appointment_type, field, value)

        appointment_type.deposit_required = deposit_required
        appointment_type.deposit_type = deposit_type
        appointment_type.deposit_value = deposit_value

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
