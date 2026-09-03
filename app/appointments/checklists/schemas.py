from marshmallow import Schema, fields, validate

from app.models.appointments.checklist_template_item import CHECKLIST_ITEM_STATUSES


class ChecklistItemMediaBriefSchema(Schema):
    """Attachment summary shown inline on a checklist item - no URL here;
    fetch GET /api/checklist-item-media/<id> for a presigned download URL."""

    id = fields.UUID(dump_only=True)
    media_type = fields.Str(dump_only=True)
    content_type = fields.Str(dump_only=True, allow_none=True)
    uploaded_at = fields.DateTime(dump_only=True, allow_none=True)


class AppointmentChecklistItemSchema(Schema):
    id = fields.UUID(dump_only=True)
    garage_id = fields.UUID(dump_only=True)
    appointment_checklist_id = fields.UUID(dump_only=True)
    checklist_template_item_id = fields.UUID(dump_only=True, allow_none=True)

    order = fields.Int(dump_only=True)
    label = fields.Str(dump_only=True)
    is_compulsory = fields.Bool(dump_only=True)
    media_type = fields.Str(dump_only=True)
    media_required_for_statuses = fields.List(fields.Str(), dump_only=True)

    status = fields.Str(validate=validate.OneOf(CHECKLIST_ITEM_STATUSES))
    notes = fields.Str(allow_none=True)
    completed_by_employee_id = fields.UUID(dump_only=True, allow_none=True)
    completed_at = fields.DateTime(dump_only=True, allow_none=True)

    media = fields.Method("_uploaded_media", dump_only=True)

    created_at = fields.DateTime(dump_only=True)
    updated_at = fields.DateTime(dump_only=True)

    def _uploaded_media(self, item):
        uploaded = [m for m in item.media if m.uploaded_at is not None]
        return ChecklistItemMediaBriefSchema(many=True).dump(uploaded)


class AppointmentChecklistItemUpdateSchema(Schema):
    status = fields.Str(validate=validate.OneOf(CHECKLIST_ITEM_STATUSES))
    notes = fields.Str(allow_none=True)


class AppointmentChecklistSchema(Schema):
    id = fields.UUID(dump_only=True)
    garage_id = fields.UUID(dump_only=True)
    appointment_id = fields.UUID(dump_only=True)
    checklist_template_id = fields.UUID(dump_only=True, allow_none=True)

    items = fields.List(fields.Nested(AppointmentChecklistItemSchema), dump_only=True)

    created_at = fields.DateTime(dump_only=True)
    updated_at = fields.DateTime(dump_only=True)
