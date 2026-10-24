from marshmallow import Schema, ValidationError, fields, validate, validates_schema

from app.appointments.add_ons.schemas import AddOnSelectionSchema
from app.models.loyalty.discount_code import DISCOUNT_CODE_STATUSES, DISCOUNT_SOURCES
from app.models.loyalty.loyalty_settings import DISCOUNT_TYPES

from .service import DERIVED_STATUSES


class LoyaltySettingsSchema(Schema):
    enabled = fields.Bool(required=True)
    visits_required = fields.Int(required=True, validate=validate.Range(min=1, max=100))
    discount_type = fields.Str(required=True, validate=validate.OneOf(DISCOUNT_TYPES))
    discount_value = fields.Decimal(required=True, as_string=True, places=2)
    expiry_days = fields.Int(required=True, validate=validate.Range(min=1, max=3650))
    enabled_at = fields.DateTime(dump_only=True, allow_none=True)


class _CodeCustomerSchema(Schema):
    id = fields.UUID(dump_only=True)
    first_name = fields.Str(dump_only=True)
    last_name = fields.Str(dump_only=True)
    email = fields.Str(dump_only=True, allow_none=True)
    marketing_email_opt_out = fields.Bool(dump_only=True)


class DiscountCodeSchema(Schema):
    id = fields.UUID(dump_only=True)
    code = fields.Str(dump_only=True)
    source = fields.Str(dump_only=True)
    discount_type = fields.Str(dump_only=True)
    discount_value = fields.Decimal(dump_only=True, as_string=True)
    # ACTIVE | DISABLED - what the owner set.
    stored_status = fields.Str(dump_only=True, attribute="status")
    # ACTIVE | USED | EXPIRED | DISABLED - derived from redemptions and dates.
    status = fields.Str(dump_only=True, attribute="_derived_status")
    uses = fields.Int(dump_only=True, attribute="_uses")
    max_uses = fields.Int(dump_only=True, allow_none=True)
    max_uses_per_customer = fields.Int(dump_only=True, allow_none=True)
    valid_from = fields.DateTime(dump_only=True, allow_none=True)
    expires_at = fields.DateTime(dump_only=True, allow_none=True)
    customer = fields.Nested(_CodeCustomerSchema, dump_only=True, allow_none=True)
    issued_for_visit_count = fields.Int(dump_only=True, allow_none=True)
    # SENT | FAILED | NO_EMAIL | OPTED_OUT | null (loyalty codes only).
    email_status = fields.Str(dump_only=True, allow_none=True)
    emailed_at = fields.DateTime(dump_only=True, allow_none=True)
    created_at = fields.DateTime(dump_only=True)


class DiscountCodePageSchema(Schema):
    items = fields.List(fields.Nested(DiscountCodeSchema), dump_only=True)
    total = fields.Int(dump_only=True)


class DiscountCodeQueryArgsSchema(Schema):
    source = fields.Str(load_default=None, validate=validate.OneOf(DISCOUNT_SOURCES))
    status = fields.Str(load_default=None, validate=validate.OneOf(DERIVED_STATUSES))
    search = fields.Str(load_default=None, validate=validate.Length(max=100))
    customer_id = fields.UUID(load_default=None)
    limit = fields.Int(load_default=25, validate=validate.Range(min=1, max=100))
    offset = fields.Int(load_default=0, validate=validate.Range(min=0))


def _check_window(data):
    valid_from = data.get("valid_from")
    expires_at = data.get("expires_at")
    if valid_from is not None and expires_at is not None and expires_at <= valid_from:
        raise ValidationError("expires_at must be after valid_from.", "expires_at")


class DiscountCodeCreateSchema(Schema):
    # Omit (or leave blank) to have one generated.
    code = fields.Str(load_default=None, allow_none=True, validate=validate.Length(max=40))
    discount_type = fields.Str(required=True, validate=validate.OneOf(DISCOUNT_TYPES))
    discount_value = fields.Decimal(required=True, as_string=True)
    valid_from = fields.DateTime(load_default=None, allow_none=True)
    expires_at = fields.DateTime(load_default=None, allow_none=True)
    max_uses = fields.Int(load_default=None, allow_none=True, validate=validate.Range(min=1))
    max_uses_per_customer = fields.Int(
        load_default=None, allow_none=True, validate=validate.Range(min=1)
    )

    @validates_schema
    def _window(self, data, **kwargs):
        _check_window(data)


class DiscountCodeUpdateSchema(Schema):
    """Limits, dates and on/off only. What a code is worth is fixed once
    created - bookings that already used it keep their own snapshot anyway."""

    status = fields.Str(validate=validate.OneOf(DISCOUNT_CODE_STATUSES))
    valid_from = fields.DateTime(allow_none=True)
    expires_at = fields.DateTime(allow_none=True)
    max_uses = fields.Int(allow_none=True, validate=validate.Range(min=1))
    max_uses_per_customer = fields.Int(allow_none=True, validate=validate.Range(min=1))


class DiscountEvaluateSchema(Schema):
    """Staff preview for the appointment form."""

    code = fields.Str(required=True, validate=validate.Length(min=1, max=60))
    customer_id = fields.UUID(required=True)
    appointment_type_id = fields.UUID(required=True)
    add_ons = fields.List(fields.Nested(AddOnSelectionSchema), load_default=list)
    # When editing: the appointment whose own redemption must not count
    # against the code, and whose add-ons are grandfathered.
    appointment_id = fields.UUID(load_default=None, allow_none=True)


class DiscountPreviewSchema(Schema):
    code = fields.Str(dump_only=True)
    discount_type = fields.Str(dump_only=True)
    discount_value = fields.Decimal(dump_only=True, as_string=True)
    original_total = fields.Decimal(dump_only=True, as_string=True)
    discount_amount = fields.Decimal(dump_only=True, as_string=True)
    new_total = fields.Decimal(dump_only=True, as_string=True)


class LoyaltySummarySchema(Schema):
    enabled = fields.Bool(dump_only=True)
    visits_required = fields.Int(dump_only=True, allow_none=True)
    completed_visits = fields.Int(dump_only=True)
    visits_to_next_reward = fields.Int(dump_only=True, allow_none=True)
    marketing_email_opt_out = fields.Bool(dump_only=True)
    codes = fields.List(fields.Nested(DiscountCodeSchema), dump_only=True)


class UnsubscribeTokenSchema(Schema):
    token = fields.Str(required=True, validate=validate.Length(min=10, max=500))


class UnsubscribeResultSchema(Schema):
    business_name = fields.Str(dump_only=True)
    unsubscribed = fields.Bool(dump_only=True)
