from marshmallow import Schema, fields, validate


class CustomerReferenceLoginSchema(Schema):
    """Email + the booking reference from a confirmation screen or email -
    knowledge-factor auth, no password involved."""

    email = fields.Email(required=True)
    booking_reference = fields.Str(
        required=True,
        load_only=True,
        validate=validate.Length(min=1, max=16),
    )


class CustomerPasswordLoginSchema(Schema):
    email = fields.Email(required=True)
    password = fields.Str(required=True, load_only=True, validate=validate.Length(min=1, max=128))


class CustomerSetPasswordSchema(Schema):
    """Body for CustomerSetPassword - lets an already-signed-in customer (via
    either login method) start using email + password going forward."""

    password = fields.Str(required=True, load_only=True, validate=validate.Length(min=8, max=128))


class CustomerTokenSchema(Schema):
    access_token = fields.Str()
    refresh_token = fields.Str()


class CustomerRefreshTokenSchema(Schema):
    access_token = fields.Str()
