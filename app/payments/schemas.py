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
    # Null until stripe_charges_enabled is true - see
    # app/payments/connect.py::get_wallet_domain_status. Registering the
    # domain doesn't mean Apple Pay is immediately usable; Stripe verifies
    # domain ownership asynchronously, so this can briefly show "pending"
    # right after the account first becomes chargeable.
    apple_pay_status = fields.Str(dump_only=True, allow_none=True)
    apple_pay_status_details = fields.Str(dump_only=True, allow_none=True)


class StripeConnectLinkSchema(Schema):
    url = fields.Str(dump_only=True)
