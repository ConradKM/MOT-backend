from marshmallow import Schema, fields, validate

from app.models.employee import ROLES


class EmployeeSchema(Schema):
    id = fields.Int(dump_only=True)
    garage_id = fields.Int(dump_only=True)

    email = fields.Email(required=True)
    password = fields.Str(
        required=True, load_only=True, validate=validate.Length(min=8)
    )
    role = fields.Str(load_default="STAFF", validate=validate.OneOf(ROLES))

    created_at = fields.DateTime(dump_only=True)
    updated_at = fields.DateTime(dump_only=True)
