from marshmallow import Schema, fields


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
    description = fields.Str(dump_only=True, allow_none=True)
    is_compulsory = fields.Bool(dump_only=True)
    media_type = fields.Str(dump_only=True)
    media_required_for_statuses = fields.List(fields.Str(), dump_only=True)
    # The values this specific logged item accepts for `status` - snapshotted
    # at instance-creation time, so it never changes even if the template
    # item's own result_options are edited afterwards (see model docstring).
    result_options = fields.List(fields.Str(), dump_only=True)
    visible_to_customer = fields.Bool(dump_only=True)

    # No fixed OneOf here - valid values are this item's own result_options,
    # checked in the route (see checklists/routes.py) since marshmallow can't
    # validate against per-instance data declaratively.
    status = fields.Str()
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
    status = fields.Str()
    notes = fields.Str(allow_none=True)


class AppointmentChecklistSchema(Schema):
    id = fields.UUID(dump_only=True)
    garage_id = fields.UUID(dump_only=True)
    appointment_id = fields.UUID(dump_only=True)
    checklist_template_id = fields.UUID(dump_only=True, allow_none=True)

    items = fields.List(fields.Nested(AppointmentChecklistItemSchema), dump_only=True)

    created_at = fields.DateTime(dump_only=True)
    updated_at = fields.DateTime(dump_only=True)
