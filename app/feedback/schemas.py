from marshmallow import Schema, fields, validate

from app.models.feedback import FEEDBACK_PRIORITIES, FEEDBACK_TYPES, PRIORITY_NORMAL


class FeedbackCreateSchema(Schema):
    """Body for POST /api/feedback.

    Deliberately has no business_id/user_id/done field - those are derived
    from the authenticated employee (see app/feedback/routes.py), never from
    the client, so a business can't submit feedback under another tenant or
    mark its own feedback done on arrival.
    """

    type = fields.Str(required=True, validate=validate.OneOf(FEEDBACK_TYPES))
    subject = fields.Str(load_default=None, allow_none=True, validate=validate.Length(max=200))
    message = fields.Str(required=True, validate=validate.Length(min=1, max=5000))
    priority = fields.Str(
        load_default=PRIORITY_NORMAL, validate=validate.OneOf(FEEDBACK_PRIORITIES)
    )
    # Optional override of the signed-in employee's own email - the dashboard
    # form pre-fills it but lets the user edit it before sending.
    email = fields.Email(load_default=None, allow_none=True)


class FeedbackSchema(Schema):
    id = fields.UUID(dump_only=True)
    business_name = fields.Str(dump_only=True)
    user_email = fields.Str(dump_only=True)
    type = fields.Str(dump_only=True)
    subject = fields.Str(dump_only=True, allow_none=True)
    message = fields.Str(dump_only=True)
    priority = fields.Str(dump_only=True)
    done = fields.Bool(dump_only=True)
    created_at = fields.DateTime(dump_only=True)
    updated_at = fields.DateTime(dump_only=True)
