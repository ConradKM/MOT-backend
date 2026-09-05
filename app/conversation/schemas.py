from marshmallow import Schema, fields, validate

from app.models.conversation.conversation_session import CHANNELS


class SimulateMessageSchema(Schema):
    channel = fields.Str(required=True, validate=validate.OneOf(CHANNELS))
    phone = fields.Str(required=True, validate=validate.Length(min=1, max=40))
    text = fields.Str(required=True, validate=validate.Length(min=1, max=2000))


class ConversationResultSchema(Schema):
    session_id = fields.UUID(dump_only=True)
    response_text = fields.Str(dump_only=True, allow_none=True)
    intent = fields.Str(dump_only=True)
    workflow_step = fields.Str(dump_only=True, allow_none=True)
    actions_performed = fields.List(fields.Str(), dump_only=True)
    needs_human = fields.Bool(dump_only=True)
    duplicate = fields.Bool(dump_only=True)
