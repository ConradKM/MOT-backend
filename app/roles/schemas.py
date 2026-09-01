from marshmallow import Schema, fields, validate


class RoleSchema(Schema):
    id = fields.UUID(dump_only=True)
    garage_id = fields.UUID(dump_only=True)
    name = fields.Str(required=True, validate=validate.Length(min=1, max=50))

    created_at = fields.DateTime(dump_only=True)
    updated_at = fields.DateTime(dump_only=True)


class RoleUpdateSchema(Schema):
    name = fields.Str(required=True, validate=validate.Length(min=1, max=50))
