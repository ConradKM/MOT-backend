import re

from marshmallow import Schema, fields, post_load, validate


class AppointmentStatusSchema(Schema):
    id = fields.UUID(dump_only=True)
    garage_id = fields.UUID(dump_only=True)
    key = fields.Str(dump_only=True)
    label = fields.Str(required=True, validate=validate.Length(min=1, max=50))
    color = fields.Str(required=True, validate=validate.Length(min=1, max=20))
    sort_order = fields.Int(load_default=0)
    is_terminal = fields.Bool(load_default=False)
    is_system = fields.Bool(dump_only=True)

    created_at = fields.DateTime(dump_only=True)
    updated_at = fields.DateTime(dump_only=True)

    # Accept an optional `key` on create; otherwise derive it from the label.
    key_in = fields.Str(
        data_key="key",
        load_only=True,
        load_default=None,
        validate=validate.Length(max=30),
    )

    @post_load
    def _derive_key(self, data, **kwargs):
        if not data.get("key_in") and data.get("label"):
            slug = re.sub(r"[^A-Z0-9]+", "_", data["label"].upper()).strip("_")
            data["key_in"] = slug[:30] or "STATUS"
        return data


class AppointmentStatusUpdateSchema(Schema):
    label = fields.Str(validate=validate.Length(min=1, max=50))
    color = fields.Str(validate=validate.Length(min=1, max=20))
    sort_order = fields.Int()
    is_terminal = fields.Bool()
