from marshmallow import Schema, fields, validate


class CustomerLoginSchema(Schema):
    email = fields.Email(required=True)
    registration_number = fields.Str(
        required=True,
        load_only=True,
        validate=validate.Length(min=1, max=20),
    )


class CustomerTokenSchema(Schema):
    access_token = fields.Str()
    refresh_token = fields.Str()


class CustomerRefreshTokenSchema(Schema):
    access_token = fields.Str()
