"""Marshmallow schemas for app/payments/routes.py - the garage-facing Stripe/Square
Connect onboarding/status API. Deliberately separate from
app/public_booking/schemas.py's deposit-intent schemas: those describe an
unauthenticated customer-facing checkout session, these describe an
authenticated business's own account connection.
"""

from marshmallow import Schema, fields, validate


class StripeConnectStatusSchema(Schema):
    provider = fields.Str(dump_only=True, allow_none=True)
    stripe_account_id = fields.Str(dump_only=True, allow_none=True)
    stripe_onboarding_complete = fields.Bool(dump_only=True)
    stripe_charges_enabled = fields.Bool(dump_only=True)
    stripe_payouts_enabled = fields.Bool(dump_only=True)
    # Null until stripe_charges_enabled is true - see
    # app/payments/connect.py::get_wallet_domain_status. Registering the
    # domain doesn't mean Apple Pay is immediately usable; Stripe verifies
    # domain ownership asynchronously, so this can briefly show "pending"
    # right after the account first becomes chargeable.
    apple_pay_status = fields.Str(dump_only=True, allow_none=True)
    apple_pay_status_details = fields.Str(dump_only=True, allow_none=True)


class StripeConnectLinkSchema(Schema):
    url = fields.Str(dump_only=True)


class SquareConnectLinkSchema(Schema):
    url = fields.Str(dump_only=True)


class SquareLocationSchema(Schema):
    id = fields.Str(dump_only=True)
    name = fields.Str(dump_only=True, allow_none=True)
    currency = fields.Str(dump_only=True, allow_none=True)
    can_take_card_payments = fields.Bool(dump_only=True)
    # Active, card-capable, and in the business's deposit currency - only
    # these can be chosen.
    eligible = fields.Bool(dump_only=True)


class SquareConnectStatusSchema(Schema):
    """Never carries a token - only ids, names and readiness flags."""

    status = fields.Str(dump_only=True)  # NOT_CONNECTED / CONNECTED / REVOKED / ERROR
    merchant_id = fields.Str(dump_only=True, allow_none=True)
    merchant_name = fields.Str(dump_only=True, allow_none=True)
    location_id = fields.Str(dump_only=True, allow_none=True)
    location_name = fields.Str(dump_only=True, allow_none=True)
    locations = fields.List(fields.Nested(SquareLocationSchema), dump_only=True)
    can_take_card_payments = fields.Bool(dump_only=True)
    currency = fields.Str(dump_only=True)
    charges_in_currency = fields.Bool(dump_only=True)
    # Connected + location chosen + usable token: deposits can be taken.
    ready = fields.Bool(dump_only=True)
    environment = fields.Str(dump_only=True)
    deployment_configured = fields.Bool(dump_only=True)


class SquareLocationUpdateSchema(Schema):
    location_id = fields.Str(required=True, validate=validate.Length(min=1, max=255))


class SquareDisconnectResultSchema(Schema):
    disconnected = fields.Bool(dump_only=True)
    # False when Square didn't confirm the remote revoke; local state is
    # cleared either way and the owner can also revoke in Square.
    remote_revoke_confirmed = fields.Bool(dump_only=True)


class PaymentProviderOptionSchema(Schema):
    name = fields.Str(dump_only=True)
    ready = fields.Bool(dump_only=True)


class PaymentProviderSelectionSchema(Schema):
    provider = fields.Str(required=True, validate=validate.Length(min=1, max=30))
    options = fields.List(fields.Nested(PaymentProviderOptionSchema), dump_only=True)
