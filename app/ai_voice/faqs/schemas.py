from marshmallow import Schema, fields, validate


class VoiceFAQSchema(Schema):
    id = fields.UUID(dump_only=True)
    garage_id = fields.UUID(dump_only=True)
    question = fields.Str(required=True, validate=validate.Length(min=1, max=300))
    answer = fields.Str(required=True, validate=validate.Length(min=1, max=5000))
    order = fields.Int(load_default=0, validate=validate.Range(min=0))
    is_enabled = fields.Bool(load_default=True)
    archived_at = fields.DateTime(dump_only=True, allow_none=True)
    created_at = fields.DateTime(dump_only=True)
    updated_at = fields.DateTime(dump_only=True)


class VoiceFAQUpdateSchema(Schema):
    question = fields.Str(validate=validate.Length(min=1, max=300))
    answer = fields.Str(validate=validate.Length(min=1, max=5000))
    order = fields.Int(validate=validate.Range(min=0))
    is_enabled = fields.Bool()


class VoiceFAQReorderSchema(Schema):
    ids = fields.List(fields.UUID(), required=True, validate=validate.Length(min=1))
