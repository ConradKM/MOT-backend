from marshmallow import Schema, fields, validate

from app.models.appointments.checklist_template_item import CHECKLIST_ITEM_STATUSES, MEDIA_TYPES


class ChecklistTemplateItemSchema(Schema):
    id = fields.UUID(dump_only=True)
    garage_id = fields.UUID(dump_only=True)
    checklist_template_id = fields.UUID(dump_only=True)

    order = fields.Int(load_default=0)
    label = fields.Str(required=True, validate=validate.Length(min=1, max=300))
    is_compulsory = fields.Bool(load_default=False)
    media_type = fields.Str(load_default="NONE", validate=validate.OneOf(MEDIA_TYPES))
    media_required_for_statuses = fields.List(
        fields.Str(validate=validate.OneOf(CHECKLIST_ITEM_STATUSES)),
        load_default=list,
    )

    created_at = fields.DateTime(dump_only=True)
    updated_at = fields.DateTime(dump_only=True)


class ChecklistTemplateItemUpdateSchema(Schema):
    order = fields.Int()
    label = fields.Str(validate=validate.Length(min=1, max=300))
    is_compulsory = fields.Bool()
    media_type = fields.Str(validate=validate.OneOf(MEDIA_TYPES))
    media_required_for_statuses = fields.List(
        fields.Str(validate=validate.OneOf(CHECKLIST_ITEM_STATUSES))
    )


class ChecklistTemplateSchema(Schema):
    id = fields.UUID(dump_only=True)
    garage_id = fields.UUID(dump_only=True)
    appointment_type_id = fields.UUID(dump_only=True)

    items = fields.List(fields.Nested(ChecklistTemplateItemSchema), dump_only=True)

    created_at = fields.DateTime(dump_only=True)
    updated_at = fields.DateTime(dump_only=True)
