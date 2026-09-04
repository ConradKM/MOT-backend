from marshmallow import Schema, fields, validate


class RegisterSchema(Schema):
    """Garage onboarding: creates the garage + its first OWNER account."""

    garage_name = fields.Str(required=True, validate=validate.Length(min=1, max=200))
    email = fields.Email(required=True)
    password = fields.Str(
        required=True, load_only=True, validate=validate.Length(min=8)
    )
    first_name = fields.Str(allow_none=True, validate=validate.Length(max=100))
    last_name = fields.Str(allow_none=True, validate=validate.Length(max=100))


class LoginSchema(Schema):
    email = fields.Email(required=True)
    password = fields.Str(required=True, load_only=True)


class TokenSchema(Schema):
    access_token = fields.Str()
    refresh_token = fields.Str()


class RefreshTokenSchema(Schema):
    access_token = fields.Str()


class ForgotPasswordSchema(Schema):
    email = fields.Email(required=True)


class ResetPasswordSchema(Schema):
    token = fields.Str(required=True)
    password = fields.Str(
        required=True, load_only=True, validate=validate.Length(min=8)
    )


class MessageSchema(Schema):
    message = fields.Str()


class ResetTokenStatusSchema(Schema):
    valid = fields.Bool()
