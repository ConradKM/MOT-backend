"""Request/response schemas for the service + service-group image flow.

Shared by app/appointments/types/ and app/appointments/type_groups/, which
expose an identical three-step endpoint trio over the same three columns. The
behaviour behind them is app/appointments/images.py.
"""

from marshmallow import Schema, fields, validate


class ImageUploadRequestSchema(Schema):
    content_type = fields.Str(required=True, validate=validate.Length(min=1, max=100))
    # A client's claim, re-checked against what actually landed at finalize
    # time (a presigned PUT goes straight to the bucket, so nothing server-side
    # saw these bytes). Declaring it lets an obviously oversized file be
    # rejected before any bytes move.
    size_bytes = fields.Int(allow_none=True, load_default=None, validate=validate.Range(min=1))


class ImageFinalizeSchema(Schema):
    storage_key = fields.Str(required=True, validate=validate.Length(min=1, max=500))


class ImageUploadTicketSchema(Schema):
    storage_key = fields.Str(dump_only=True)
    upload_url = fields.Str(dump_only=True)
    expires_in = fields.Int(dump_only=True)


class ReorderSchema(Schema):
    """A whole ordered list in one request.

    The alternative - a PATCH per moved row - costs two round trips per single
    move and can leave a half-applied order if the second fails. A builder UI
    reorders constantly, so the atomic version is the one worth having.
    """

    ids = fields.List(fields.UUID(), required=True, validate=validate.Length(min=1))
