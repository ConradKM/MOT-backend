from marshmallow import Schema, ValidationError, fields, validate, validates_schema

from app.models.appointments.checklist_template_item import GENERIC_RESULT_OPTIONS, MEDIA_TYPES


def _default_result_options():
    return list(GENERIC_RESULT_OPTIONS)


class ChecklistTemplateItemSchema(Schema):
    id = fields.UUID(dump_only=True)
    garage_id = fields.UUID(dump_only=True)
    checklist_template_id = fields.UUID(dump_only=True)

    order = fields.Int(load_default=0)
    label = fields.Str(required=True, validate=validate.Length(min=1, max=300))
    description = fields.Str(allow_none=True, load_default=None, validate=validate.Length(max=500))
    is_compulsory = fields.Bool(load_default=False)
    media_type = fields.Str(load_default="NONE", validate=validate.OneOf(MEDIA_TYPES))
    media_required_for_statuses = fields.List(fields.Str(), load_default=list)
    # The values staff can log against this item - no fixed platform-wide
    # enum (see model docstring). Defaults to a generic "done / not done"
    # set; a garage can switch an item to the automotive DVSA-style preset
    # (CHECKLIST_ITEM_STATUSES) or any custom list of its own.
    result_options = fields.List(
        fields.Str(validate=validate.Length(min=1, max=30)),
        load_default=_default_result_options,
        validate=validate.Length(min=1, max=20),
    )
    visible_to_customer = fields.Bool(load_default=False)

    created_at = fields.DateTime(dump_only=True)
    updated_at = fields.DateTime(dump_only=True)

    @validates_schema
    def _media_statuses_are_real_options(self, data, **kwargs):
        options = data.get("result_options") or []
        bad = [s for s in data.get("media_required_for_statuses", []) if s not in options]
        if bad:
            raise ValidationError(
                {
                    "media_required_for_statuses": [
                        f"{', '.join(bad)} - not one of this item's result_options."
                    ]
                }
            )


class ChecklistTemplateItemUpdateSchema(Schema):
    order = fields.Int()
    label = fields.Str(validate=validate.Length(min=1, max=300))
    description = fields.Str(allow_none=True, validate=validate.Length(max=500))
    is_compulsory = fields.Bool()
    media_type = fields.Str(validate=validate.OneOf(MEDIA_TYPES))
    media_required_for_statuses = fields.List(fields.Str())
    result_options = fields.List(
        fields.Str(validate=validate.Length(min=1, max=30)),
        validate=validate.Length(min=1, max=20),
    )
    visible_to_customer = fields.Bool()

    @validates_schema
    def _media_statuses_are_real_options(self, data, **kwargs):
        # Only meaningful when both are present in the same PATCH - checking
        # a partial update's media_required_for_statuses against the item's
        # *existing* result_options (when only one of the two is being
        # changed) is the route's job, since only it has the current row.
        if "result_options" not in data or "media_required_for_statuses" not in data:
            return
        bad = [s for s in data["media_required_for_statuses"] if s not in data["result_options"]]
        if bad:
            raise ValidationError(
                {
                    "media_required_for_statuses": [
                        f"{', '.join(bad)} - not one of this item's result_options."
                    ]
                }
            )


class ChecklistTemplateSchema(Schema):
    id = fields.UUID(dump_only=True)
    garage_id = fields.UUID(dump_only=True)
    appointment_type_id = fields.UUID(dump_only=True)

    items = fields.List(fields.Nested(ChecklistTemplateItemSchema), dump_only=True)

    created_at = fields.DateTime(dump_only=True)
    updated_at = fields.DateTime(dump_only=True)
