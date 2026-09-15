from flask.views import MethodView
from flask_jwt_extended import jwt_required
from flask_smorest import Blueprint, abort
from sqlalchemy.exc import IntegrityError

from app.appointments.image_schemas import (
    ImageFinalizeSchema,
    ImageUploadRequestSchema,
    ImageUploadTicketSchema,
)
from app.appointments.images import clear_image, finalize_upload, request_upload
from app.auth.decorators import owner_required
from app.auth.utils import get_current_employee
from app.extensions import db
from app.models.appointments.appointment_type import GarageAppointmentType
from app.models.appointments.appointment_type_group import AppointmentTypeGroup
from app.storage.images import ImageError, ImageNotUploadedError, delete_image

from .schemas import (
    AppointmentTypeQueryArgsSchema,
    AppointmentTypeSchema,
    AppointmentTypeUpdateSchema,
)

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


def _validate_group(group_id, garage_id):
    """Reject a group_id that isn't this business's own.

    The FK alone would happily accept another tenant's group id, which would
    then leak that group's name onto this business's booking page. None is
    always fine - it means ungrouped.
    """
    if group_id is None:
        return

    exists = AppointmentTypeGroup.query.filter_by(id=group_id, garage_id=garage_id).first()
    if exists is None:
        abort(422, message="group_id is not a group for this business.")


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

        # Display order first, name as the tie-break - so a business that has
        # never reordered anything still gets the alphabetical listing this
        # endpoint has always returned.
        return query.order_by(GarageAppointmentType.order, GarageAppointmentType.name).all()

    @jwt_required()
    @owner_required
    @appointment_types_blp.arguments(AppointmentTypeSchema)
    @appointment_types_blp.response(201, AppointmentTypeSchema)
    def post(self, data):
        garage_id = get_current_employee().garage_id
        _validate_group(data.get("group_id"), garage_id)

        appointment_type = GarageAppointmentType(
            garage_id=garage_id,
            name=data["name"],
            description=data.get("description"),
            base_price=data.get("base_price"),
            default_duration_minutes=data.get("default_duration_minutes"),
            status=data.get("status") or "ACTIVE",
            group_id=data.get("group_id"),
            order=data.get("order") or 0,
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

        if "group_id" in data:
            _validate_group(data["group_id"], garage_id)

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

        # Grab the key before the delete: afterwards the row is gone and the
        # object would be orphaned in the bucket forever. Removing the object
        # itself waits until the delete has actually committed - a 409 below
        # must leave the service completely untouched, image included.
        image_key = appointment_type.image_storage_key

        db.session.delete(appointment_type)

        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            abort(
                409,
                message="Cannot delete an appointment type that has appointments booked against it.",
            )

        delete_image(image_key)

        return ""


@appointment_types_blp.route("/<uuid:appointment_type_id>/image")
class AppointmentTypeImage(MethodView):
    @jwt_required()
    @owner_required
    @appointment_types_blp.arguments(ImageUploadRequestSchema)
    @appointment_types_blp.response(201, ImageUploadTicketSchema)
    def post(self, data, appointment_type_id):
        """Step 1: a presigned PUT ticket. Writes nothing to the service yet -
        see app/appointments/images.py for the three-step flow."""
        garage_id = get_current_employee().garage_id
        get_owned_appointment_type(appointment_type_id, garage_id)

        try:
            return request_upload(
                garage_id,
                content_type=data["content_type"],
                size_bytes=data.get("size_bytes"),
            )
        except ImageError as exc:
            abort(422, message=str(exc))

    @jwt_required()
    @owner_required
    @appointment_types_blp.arguments(ImageFinalizeSchema)
    @appointment_types_blp.response(200, AppointmentTypeSchema)
    def put(self, data, appointment_type_id):
        """Step 3: confirm the bytes landed and are really an image, then make
        it the service's live image."""
        garage_id = get_current_employee().garage_id
        appointment_type = get_owned_appointment_type(appointment_type_id, garage_id)

        try:
            return finalize_upload(appointment_type, storage_key=data["storage_key"])
        except ImageNotUploadedError as exc:
            abort(409, message=str(exc))
        except ImageError as exc:
            abort(422, message=str(exc))

    @jwt_required()
    @owner_required
    @appointment_types_blp.response(200, AppointmentTypeSchema)
    def delete(self, appointment_type_id):
        garage_id = get_current_employee().garage_id
        appointment_type = get_owned_appointment_type(appointment_type_id, garage_id)

        clear_image(appointment_type)

        return appointment_type
