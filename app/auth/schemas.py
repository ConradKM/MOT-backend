from marshmallow import Schema, fields, validate


class RegisterSchema(Schema):
    """Garage onboarding: creates the garage + its first OWNER account."""

    garage_name = fields.Str(required=True, validate=validate.Length(min=1, max=200))
    email = fields.Email(required=True)
    password = fields.Str(required=True, load_only=True, validate=validate.Length(min=8))
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
    password = fields.Str(required=True, load_only=True, validate=validate.Length(min=8))


class MessageSchema(Schema):
    message = fields.Str()


class ResetTokenStatusSchema(Schema):
    valid = fields.Bool()


class ImpersonationExchangeSchema(Schema):
    """Redeeming a Platform Admin support-impersonation handoff code.

    The code is the credential (single-use, ~90 seconds, hashed at rest), so
    this request carries no bearer token - see
    app/platform_admin/impersonation.py.
    """

    code = fields.Str(required=True, load_only=True, validate=validate.Length(min=8, max=200))


class ImpersonationGarageSchema(Schema):
    id = fields.UUID(dump_only=True)
    name = fields.Str(dump_only=True)


class ImpersonationEmployeeSchema(Schema):
    id = fields.UUID(dump_only=True)
    email = fields.Email(dump_only=True)


class ImpersonationTokenSchema(Schema):
    """The impersonation session handed to the garage frontend.

    No refresh token, on purpose: the session ends at ``expires_at`` and can
    only be renewed by an administrator starting (and re-auditing) a new one.
    """

    access_token = fields.Str(dump_only=True)
    expires_at = fields.DateTime(dump_only=True)
    garage = fields.Nested(ImpersonationGarageSchema, dump_only=True)
    employee = fields.Nested(ImpersonationEmployeeSchema, dump_only=True)
    impersonated_by_email = fields.Email(dump_only=True, allow_none=True)
