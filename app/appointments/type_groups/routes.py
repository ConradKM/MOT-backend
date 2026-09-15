from flask.views import MethodView
from flask_jwt_extended import jwt_required
from flask_smorest import Blueprint, abort

from app.appointments.image_schemas import (
    ImageFinalizeSchema,
    ImageUploadRequestSchema,
    ImageUploadTicketSchema,
    ReorderSchema,
)
from app.appointments.images import clear_image, finalize_upload, request_upload
from app.auth.decorators import owner_required
from app.auth.utils import get_current_employee
from app.extensions import db
from app.models.appointments.appointment_type import GarageAppointmentType
from app.models.appointments.appointment_type_group import AppointmentTypeGroup
from app.storage.images import ImageError, ImageNotUploadedError

from .schemas import AppointmentTypeGroupSchema, AppointmentTypeGroupUpdateSchema

appointment_type_groups_blp = Blueprint(
    "appointment-type-groups",
    "appointment-type-groups",
    url_prefix="/api/appointment-type-groups",
    description="Business-defined groupings of services, for booking-page navigation",
)


def get_owned_group(group_id, garage_id):
    group = AppointmentTypeGroup.query.filter_by(id=group_id, garage_id=garage_id).first()

    if not group:
        abort(404, message="Appointment type group not found")

    return group


@appointment_type_groups_blp.route("/")
class AppointmentTypeGroupList(MethodView):
    @jwt_required()
    @appointment_type_groups_blp.response(200, AppointmentTypeGroupSchema(many=True))
    def get(self):
        garage_id = get_current_employee().garage_id

        return (
            AppointmentTypeGroup.query.filter_by(garage_id=garage_id)
            .order_by(AppointmentTypeGroup.order, AppointmentTypeGroup.name)
            .all()
        )

    @jwt_required()
    @owner_required
    @appointment_type_groups_blp.arguments(AppointmentTypeGroupSchema)
    @appointment_type_groups_blp.response(201, AppointmentTypeGroupSchema)
    def post(self, data):
        garage_id = get_current_employee().garage_id

        group = AppointmentTypeGroup(
            garage_id=garage_id,
            name=data["name"],
            description=data.get("description"),
            order=data.get("order") or 0,
            display_mode=data.get("display_mode"),
        )

        db.session.add(group)
        db.session.commit()

        return group


@appointment_type_groups_blp.route("/order")
class AppointmentTypeGroupReorder(MethodView):
    @jwt_required()
    @owner_required
    @appointment_type_groups_blp.arguments(ReorderSchema)
    @appointment_type_groups_blp.response(200, AppointmentTypeGroupSchema(many=True))
    def put(self, data):
        """Apply a whole new order in one transaction.

        Every one of this business's groups must appear exactly once: a
        partial list would silently leave the omitted ones at stale positions,
        colliding with the new ones.
        """
        garage_id = get_current_employee().garage_id

        groups = AppointmentTypeGroup.query.filter_by(garage_id=garage_id).all()
        by_id = {g.id: g for g in groups}

        ids = data["ids"]
        if len(set(ids)) != len(ids) or set(ids) != set(by_id):
            abort(422, message="ids must list every group for this business exactly once.")

        for position, group_id in enumerate(ids):
            by_id[group_id].order = position

        db.session.commit()

        return sorted(groups, key=lambda g: (g.order, g.name))


@appointment_type_groups_blp.route("/<uuid:group_id>")
class AppointmentTypeGroupResource(MethodView):
    @jwt_required()
    @appointment_type_groups_blp.response(200, AppointmentTypeGroupSchema)
    def get(self, group_id):
        garage_id = get_current_employee().garage_id

        return get_owned_group(group_id, garage_id)

    @jwt_required()
    @owner_required
    @appointment_type_groups_blp.arguments(AppointmentTypeGroupUpdateSchema)
    @appointment_type_groups_blp.response(200, AppointmentTypeGroupSchema)
    def patch(self, data, group_id):
        garage_id = get_current_employee().garage_id
        group = get_owned_group(group_id, garage_id)

        for field, value in data.items():
            setattr(group, field, value)

        db.session.commit()

        return group

    @jwt_required()
    @owner_required
    @appointment_type_groups_blp.response(204)
    def delete(self, group_id):
        garage_id = get_current_employee().garage_id
        group = get_owned_group(group_id, garage_id)

        # Deliberately not a 409 the way deleting an in-use *service* is
        # (app/appointments/types/routes.py): a group is pure navigation, so
        # emptying it costs the business nothing. The FK is SET NULL, so its
        # services survive as ungrouped along with all their history.
        clear_image(group)
        db.session.delete(group)
        db.session.commit()

        return ""


@appointment_type_groups_blp.route("/<uuid:group_id>/services/order")
class GroupServiceReorder(MethodView):
    @jwt_required()
    @owner_required
    @appointment_type_groups_blp.arguments(ReorderSchema)
    @appointment_type_groups_blp.response(204)
    def put(self, data, group_id):
        """Order the services inside one group. Same all-or-nothing rule as
        the group reorder above."""
        garage_id = get_current_employee().garage_id
        get_owned_group(group_id, garage_id)

        members = GarageAppointmentType.query.filter_by(
            garage_id=garage_id, group_id=group_id
        ).all()
        by_id = {t.id: t for t in members}

        ids = data["ids"]
        if len(set(ids)) != len(ids) or set(ids) != set(by_id):
            abort(422, message="ids must list every service in this group exactly once.")

        for position, type_id in enumerate(ids):
            by_id[type_id].order = position

        db.session.commit()

        return ""


@appointment_type_groups_blp.route("/<uuid:group_id>/image")
class AppointmentTypeGroupImage(MethodView):
    @jwt_required()
    @owner_required
    @appointment_type_groups_blp.arguments(ImageUploadRequestSchema)
    @appointment_type_groups_blp.response(201, ImageUploadTicketSchema)
    def post(self, data, group_id):
        """Step 1: a presigned PUT ticket. Nothing is written to the group
        yet - see app/appointments/images.py."""
        garage_id = get_current_employee().garage_id
        get_owned_group(group_id, garage_id)

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
    @appointment_type_groups_blp.arguments(ImageFinalizeSchema)
    @appointment_type_groups_blp.response(200, AppointmentTypeGroupSchema)
    def put(self, data, group_id):
        """Step 3: confirm the bytes landed and are really an image, then make
        it the group's live image."""
        garage_id = get_current_employee().garage_id
        group = get_owned_group(group_id, garage_id)

        try:
            return finalize_upload(group, storage_key=data["storage_key"])
        except ImageNotUploadedError as exc:
            abort(409, message=str(exc))
        except ImageError as exc:
            abort(422, message=str(exc))

    @jwt_required()
    @owner_required
    @appointment_type_groups_blp.response(200, AppointmentTypeGroupSchema)
    def delete(self, group_id):
        garage_id = get_current_employee().garage_id
        group = get_owned_group(group_id, garage_id)

        clear_image(group)

        return group
