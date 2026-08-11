from marshmallow import Schema, fields


class CustomerSchema(Schema):
    id = fields.Int(dump_only=True)
    garage_id = fields.Int(dump_only=True)

    first_name = fields.Str(required=True)
    last_name = fields.Str(required=True)

    email = fields.Email(allow_none=True)
    phone = fields.Str(allow_none=True)


class CustomerUpdateSchema(Schema):
    first_name = fields.Str()
    last_name = fields.Str()
    email = fields.Email(allow_none=True)
    phone = fields.Str(allow_none=True)