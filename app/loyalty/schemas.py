from marshmallow import Schema, fields, validate

from app.models.loyalty.ledger import ENTRY_TYPES, SOURCE_TYPES
from app.models.loyalty.program import PROGRAM_TYPES, REWARD_TYPES
from app.models.loyalty.reward import REWARD_STATUSES


class LoyaltyProgramSchema(Schema):
    id = fields.UUID(dump_only=True)
    garage_id = fields.UUID(dump_only=True)

    enabled = fields.Bool()
    name = fields.Str(validate=validate.Length(min=1, max=100))
    description = fields.Str(allow_none=True, validate=validate.Length(max=2000))

    program_type = fields.Str(validate=validate.OneOf(PROGRAM_TYPES))
    earn_per_visit = fields.Int(validate=validate.Range(min=1))
    threshold = fields.Int(validate=validate.Range(min=1))

    reward_type = fields.Str(validate=validate.OneOf(REWARD_TYPES))
    reward_value_minor = fields.Int(validate=validate.Range(min=0))
    currency = fields.Str(validate=validate.Length(equal=3))

    qualifying_appointment_type_ids = fields.List(fields.UUID(), allow_none=True)
    min_spend_minor = fields.Int(allow_none=True, validate=validate.Range(min=0))

    created_at = fields.DateTime(dump_only=True)
    updated_at = fields.DateTime(dump_only=True)


class LoyaltyProgramUpdateSchema(Schema):
    enabled = fields.Bool()
    name = fields.Str(validate=validate.Length(min=1, max=100))
    description = fields.Str(allow_none=True, validate=validate.Length(max=2000))
    program_type = fields.Str(validate=validate.OneOf(PROGRAM_TYPES))
    earn_per_visit = fields.Int(validate=validate.Range(min=1))
    threshold = fields.Int(validate=validate.Range(min=1))
    reward_type = fields.Str(validate=validate.OneOf(REWARD_TYPES))
    reward_value_minor = fields.Int(validate=validate.Range(min=0))
    currency = fields.Str(validate=validate.Length(equal=3))
    qualifying_appointment_type_ids = fields.List(fields.UUID(), allow_none=True)
    min_spend_minor = fields.Int(allow_none=True, validate=validate.Range(min=0))


class LoyaltyRewardSchema(Schema):
    id = fields.UUID(dump_only=True)
    cycle_number = fields.Int(dump_only=True)
    status = fields.Str(dump_only=True, validate=validate.OneOf(REWARD_STATUSES))
    reward_type = fields.Str(dump_only=True)
    reward_value_minor = fields.Int(dump_only=True)
    currency = fields.Str(dump_only=True)
    created_at = fields.DateTime(dump_only=True)
    redeemed_at = fields.DateTime(dump_only=True, allow_none=True)


class LoyaltyProgressSchema(Schema):
    enabled = fields.Bool(dump_only=True)
    program_name = fields.Str(dump_only=True)
    description = fields.Str(dump_only=True, allow_none=True)
    current_units = fields.Int(dump_only=True)
    target = fields.Int(dump_only=True)
    remaining = fields.Int(dump_only=True)
    lifetime_units = fields.Int(dump_only=True)
    reward_available = fields.Bool(dump_only=True)
    reward_type = fields.Str(dump_only=True)
    reward_value_minor = fields.Int(dump_only=True)
    currency = fields.Str(dump_only=True)
    available_rewards = fields.List(fields.Nested(LoyaltyRewardSchema), dump_only=True)


class LoyaltyLedgerEntrySchema(Schema):
    id = fields.UUID(dump_only=True)
    entry_type = fields.Str(dump_only=True, validate=validate.OneOf(ENTRY_TYPES))
    units = fields.Int(dump_only=True)
    source_type = fields.Str(dump_only=True, validate=validate.OneOf(SOURCE_TYPES))
    source_id = fields.Str(dump_only=True, allow_none=True)
    reason = fields.Str(dump_only=True, allow_none=True)
    created_at = fields.DateTime(dump_only=True)


class LoyaltyAdjustSchema(Schema):
    delta = fields.Int(required=True)
    reason = fields.Str(required=True, validate=validate.Length(min=1, max=500))
