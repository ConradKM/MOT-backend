from flask.views import MethodView
from flask_jwt_extended import jwt_required
from flask_smorest import Blueprint, abort

from app.appointments.types.routes import get_owned_appointment_type
from app.auth.decorators import owner_required
from app.auth.utils import get_current_employee
from app.extensions import db
from app.models.appointments.checklist_template import ChecklistTemplate
from app.models.appointments.checklist_template_item import ChecklistTemplateItem

from .schemas import (
    ChecklistTemplateItemSchema,
    ChecklistTemplateItemUpdateSchema,
    ChecklistTemplateSchema,
)

checklist_templates_blp = Blueprint(
    "checklist-templates",
    "checklist-templates",
    url_prefix="/api/appointment-types/<uuid:appointment_type_id>/checklist-template",
    description="The one checklist template for an appointment type, and its items",
)


def _get_template_or_404(appointment_type_id, garage_id):
    template = ChecklistTemplate.query.filter_by(
        appointment_type_id=appointment_type_id, garage_id=garage_id
    ).first()

    if not template:
        abort(404, message="No checklist template exists for this appointment type yet.")

    return template


@checklist_templates_blp.route("")
class ChecklistTemplateResource(MethodView):

    @jwt_required()
    @checklist_templates_blp.response(200, ChecklistTemplateSchema)
    def get(self, appointment_type_id):
        garage_id = get_current_employee().garage_id
        get_owned_appointment_type(appointment_type_id, garage_id)

        return _get_template_or_404(appointment_type_id, garage_id)

    @jwt_required()
    @owner_required
    @checklist_templates_blp.response(201, ChecklistTemplateSchema)
    def post(self, appointment_type_id):
        garage_id = get_current_employee().garage_id
        appointment_type = get_owned_appointment_type(appointment_type_id, garage_id)

        if appointment_type.checklist_template is not None:
            abort(409, message="This appointment type already has a checklist template.")

        template = ChecklistTemplate(garage_id=garage_id, appointment_type_id=appointment_type.id)
        db.session.add(template)
        db.session.commit()

        return template

    @jwt_required()
    @owner_required
    @checklist_templates_blp.response(204)
    def delete(self, appointment_type_id):
        garage_id = get_current_employee().garage_id
        get_owned_appointment_type(appointment_type_id, garage_id)
        template = _get_template_or_404(appointment_type_id, garage_id)

        db.session.delete(template)
        db.session.commit()

        return ""


@checklist_templates_blp.route("/items")
class ChecklistTemplateItemList(MethodView):

    @jwt_required()
    @checklist_templates_blp.response(200, ChecklistTemplateItemSchema(many=True))
    def get(self, appointment_type_id):
        garage_id = get_current_employee().garage_id
        get_owned_appointment_type(appointment_type_id, garage_id)
        template = _get_template_or_404(appointment_type_id, garage_id)

        return template.items

    @jwt_required()
    @owner_required
    @checklist_templates_blp.arguments(ChecklistTemplateItemSchema)
    @checklist_templates_blp.response(201, ChecklistTemplateItemSchema)
    def post(self, data, appointment_type_id):
        garage_id = get_current_employee().garage_id
        get_owned_appointment_type(appointment_type_id, garage_id)
        template = _get_template_or_404(appointment_type_id, garage_id)

        item = ChecklistTemplateItem(
            garage_id=garage_id,
            checklist_template_id=template.id,
            order=data["order"],
            label=data["label"],
            is_compulsory=data["is_compulsory"],
            media_type=data["media_type"],
            media_required_for_statuses=data["media_required_for_statuses"],
        )
        db.session.add(item)
        db.session.commit()

        return item


@checklist_templates_blp.route("/items/<uuid:item_id>")
class ChecklistTemplateItemResource(MethodView):

    def _get_owned_item(self, appointment_type_id, garage_id, item_id):
        get_owned_appointment_type(appointment_type_id, garage_id)
        template = _get_template_or_404(appointment_type_id, garage_id)

        item = ChecklistTemplateItem.query.filter_by(
            id=item_id, checklist_template_id=template.id
        ).first()

        if not item:
            abort(404, message="Checklist template item not found")

        return item

    @jwt_required()
    @checklist_templates_blp.response(200, ChecklistTemplateItemSchema)
    def get(self, appointment_type_id, item_id):
        garage_id = get_current_employee().garage_id
        return self._get_owned_item(appointment_type_id, garage_id, item_id)

    @jwt_required()
    @owner_required
    @checklist_templates_blp.arguments(ChecklistTemplateItemUpdateSchema)
    @checklist_templates_blp.response(200, ChecklistTemplateItemSchema)
    def patch(self, data, appointment_type_id, item_id):
        garage_id = get_current_employee().garage_id
        item = self._get_owned_item(appointment_type_id, garage_id, item_id)

        for field, value in data.items():
            setattr(item, field, value)

        db.session.commit()

        return item

    @jwt_required()
    @owner_required
    @checklist_templates_blp.response(204)
    def delete(self, appointment_type_id, item_id):
        garage_id = get_current_employee().garage_id
        item = self._get_owned_item(appointment_type_id, garage_id, item_id)

        db.session.delete(item)
        db.session.commit()

        return ""
