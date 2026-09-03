from marshmallow import Schema, fields, validate

# Accepted upload content types per media_type. Anything else -> 422.
ALLOWED_CONTENT_TYPES = {
    "PHOTO": {"image/jpeg", "image/png", "image/webp", "image/heic"},
    "VIDEO": {"video/mp4", "video/quicktime", "video/webm"},
}


class MediaCreateSchema(Schema):
    media_type = fields.Str(required=True, validate=validate.OneOf(["PHOTO", "VIDEO"]))
    content_type = fields.Str(required=True, validate=validate.Length(min=1, max=100))
    original_filename = fields.Str(
        allow_none=True, load_default=None, validate=validate.Length(max=255)
    )
    size_bytes = fields.Int(allow_none=True, load_default=None, validate=validate.Range(min=1))


class MediaFinalizeSchema(Schema):
    size_bytes = fields.Int(allow_none=True, load_default=None, validate=validate.Range(min=1))


class MediaUploadTicketSchema(Schema):
    """Returned when an upload URL is issued."""

    id = fields.UUID(dump_only=True)
    storage_key = fields.Str(dump_only=True)
    upload_url = fields.Str(dump_only=True)
    expires_in = fields.Int(dump_only=True)


class MediaSchema(Schema):
    id = fields.UUID(dump_only=True)
    appointment_checklist_item_id = fields.UUID(dump_only=True)
    media_type = fields.Str(dump_only=True)
    content_type = fields.Str(dump_only=True, allow_none=True)
    size_bytes = fields.Int(dump_only=True, allow_none=True)
    original_filename = fields.Str(dump_only=True, allow_none=True)
    uploaded_at = fields.DateTime(dump_only=True, allow_none=True)
    created_at = fields.DateTime(dump_only=True)


class MediaWithDownloadSchema(MediaSchema):
    download_url = fields.Str(dump_only=True)
    expires_in = fields.Int(dump_only=True)
