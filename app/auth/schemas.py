from marshmallow import Schema, fields


class RegisterSchema(Schema):
    garage_name = fields.Str(required=True)
    email = fields.Email(required=True)
    password = fields.Str(required=True, load_only=True)


class LoginSchema(Schema):
    email = fields.Email(required=True)
    password = fields.Str(required=True, load_only=True)


class TokenSchema(Schema):
    access_token = fields.Str()
    refresh_token = fields.Str()

class RefreshTokenSchema(Schema):
    access_token = fields.Str()