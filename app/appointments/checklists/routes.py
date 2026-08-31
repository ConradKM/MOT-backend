from datetime import UTC, datetime

from flask.views import MethodView
from flask_jwt_extended import jwt_required
from flask_smorest import Blueprint, abort

from app.auth.utils import get_current_employee
from app.extensions import db
from app.models.appointments.appointment import Appointment
from app.models.appointments.appointment_checklist import AppointmentChecklist
from app.models.appointments.appointment_checklist_item import AppointmentChecklistItem

from .schemas import (
    AppointmentChecklistItemSchema,
    AppointmentChecklistItemUpdateSchema,
    AppointmentChecklistSchema,
)

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

        template = appointment.appointment_type.checklist_template
        if not template:
            abort(422, message="This appointment type has no checklist template defined yet.")

        checklist = AppointmentChecklist(
            garage_id=garage_id,
            appointment_id=appointment.id,
            checklist_template_id=template.id,
        )
        db.session.add(checklist)
        db.session.flush()

        for template_item in template.items:
            db.session.add(
                AppointmentChecklistItem(
                    garage_id=garage_id,
                    appointment_checklist_id=checklist.id,
                    checklist_template_item_id=template_item.id,
                    order=template_item.order,
                    label=template_item.label,
                    is_compulsory=template_item.is_compulsory,
                    media_type=template_item.media_type,
                    media_required_for_statuses=list(template_item.media_required_for_statuses),
                )
            )

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

        for field, value in data.items():
            setattr(item, field, value)

        if "status" in data:
            item.completed_by_employee_id = get_current_employee().id
            item.completed_at = datetime.now(UTC)

        db.session.commit()

        return item
