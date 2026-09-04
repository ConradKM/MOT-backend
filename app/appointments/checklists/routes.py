from datetime import UTC, datetime

from flask.views import MethodView
from flask_jwt_extended import jwt_required
from flask_smorest import Blueprint, abort

from app.auth.utils import get_current_employee
from app.extensions import db
from app.models.appointments.appointment import Appointment
from app.models.appointments.appointment_checklist_item import AppointmentChecklistItem

from .schemas import (
    AppointmentChecklistItemSchema,
    AppointmentChecklistItemUpdateSchema,
    AppointmentChecklistSchema,
)
from .service import snapshot_checklist_for_appointment

appointment_checklists_blp = Blueprint(
    "appointment-checklists",
    "appointment-checklists",
    url_prefix="/api",
    description="Per-appointment checklist instances, snapshotted from the "
    "appointment type's checklist template the first time they're opened.",
)


def _get_owned_appointment(appointment_id, garage_id):
    appointment = Appointment.query.filter_by(id=appointment_id, garage_id=garage_id).first()

    if not appointment:
        abort(404, message="Appointment not found")

    return appointment


@appointment_checklists_blp.route("/appointments/<uuid:appointment_id>/checklist")
class AppointmentChecklistResource(MethodView):

    @jwt_required()
    @appointment_checklists_blp.response(200, AppointmentChecklistSchema)
    def get(self, appointment_id):
        garage_id = get_current_employee().garage_id
        appointment = _get_owned_appointment(appointment_id, garage_id)

        if not appointment.checklist:
            abort(404, message="No checklist has been started for this appointment yet.")

        return appointment.checklist

    @jwt_required()
    @appointment_checklists_blp.response(201, AppointmentChecklistSchema)
    def post(self, appointment_id):
        garage_id = get_current_employee().garage_id
        appointment = _get_owned_appointment(appointment_id, garage_id)

        if appointment.checklist:
            abort(409, message="A checklist has already been started for this appointment.")

        if appointment.appointment_type.checklist_template is None:
            abort(422, message="This appointment type has no checklist template defined yet.")

        checklist = snapshot_checklist_for_appointment(appointment)
        db.session.commit()

        return checklist


@appointment_checklists_blp.route(
    "/appointment-checklists/<uuid:checklist_id>/items/<uuid:item_id>"
)
class AppointmentChecklistItemResource(MethodView):

    @jwt_required()
    @appointment_checklists_blp.arguments(AppointmentChecklistItemUpdateSchema)
    @appointment_checklists_blp.response(200, AppointmentChecklistItemSchema)
    def patch(self, data, checklist_id, item_id):
        garage_id = get_current_employee().garage_id

        item = AppointmentChecklistItem.query.filter_by(
            id=item_id, appointment_checklist_id=checklist_id, garage_id=garage_id
        ).first()

        if not item:
            abort(404, message="Checklist item not found")

        if "status" in data and data["status"] not in item.result_options:
            abort(
                422,
                message=f"'{data['status']}' is not a valid result for this item "
                f"- choose one of: {', '.join(item.result_options)}.",
            )

        for field, value in data.items():
            setattr(item, field, value)

        if "status" in data:
            item.completed_by_employee_id = get_current_employee().id
            item.completed_at = datetime.now(UTC)

        db.session.commit()

        return item
