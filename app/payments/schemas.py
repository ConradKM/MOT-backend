"""Marshmallow schemas for app/payments/routes.py - the garage-facing Stripe
Connect onboarding/status API. Deliberately separate from
app/public_booking/schemas.py's deposit-intent schemas: those describe an
unauthenticated customer-facing checkout session, these describe an
authenticated business's own account connection.
"""

from marshmallow import Schema, fields


class StripeConnectStatusSchema(Schema):
    provider = fields.Str(dump_only=True, allow_none=True)
    stripe_account_id = fields.Str(dump_only=True, allow_none=True)
    stripe_onboarding_complete = fields.Bool(dump_only=True)
    stripe_charges_enabled = fields.Bool(dump_only=True)
    stripe_payouts_enabled = fields.Bool(dump_only=True)


class StripeConnectLinkSchema(Schema):
    url = fields.Str(dump_only=True)
