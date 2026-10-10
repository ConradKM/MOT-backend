from marshmallow import EXCLUDE, Schema, fields, validate

from app.appointments.add_ons.schemas import AppliedAddOnSchema

from .service import MAX_TERMS_LENGTH


class GarageTermsSchema(Schema):
    """The business's current terms, as the settings page edits them."""

    # Null when the business has none (terms are then switched off).
    terms_and_conditions = fields.Str(dump_only=True, allow_none=True)
    # The current wording's version; null until terms are first saved.
    terms_version = fields.Int(dump_only=True, allow_none=True)
    # When the current wording was saved - i.e. when it took effect.
    terms_updated_at = fields.DateTime(dump_only=True, allow_none=True)


class GarageTermsUpdateSchema(Schema):
    # Plain text, stored trimmed; empty/whitespace-only (or null) turns terms
    # off. Over-long text is a 422 here rather than a truncation.
    terms_and_conditions = fields.Str(
        required=True, allow_none=True, validate=validate.Length(max=MAX_TERMS_LENGTH)
    )


class PublicTermsSchema(Schema):
    """One wording of a business's terms, for the public Terms page."""

    garage_id = fields.UUID(dump_only=True)
    garage_name = fields.Str(dump_only=True)
    logo_url = fields.Str(dump_only=True, allow_none=True)
    version = fields.Int(dump_only=True)
    # Always plain text - the page renders it as text, never as HTML.
    body = fields.Str(dump_only=True)
    effective_at = fields.DateTime(dump_only=True)
    # Whether this is still the business's current wording - a versioned
    # link from an old confirmation can say "this has since been updated".
    is_current = fields.Bool(dump_only=True)


class TermsAcceptanceTokenSchema(Schema):
    """The texted acceptance capability, posted in the body - never in a URL
    the API sees (see app/terms/service.py::acceptance_url)."""

    class Meta:
        unknown = EXCLUDE

    token = fields.Str(required=True, validate=validate.Length(min=32, max=256))


class TermsAcceptanceSubmitSchema(TermsAcceptanceTokenSchema):
    terms_accepted = fields.Bool(load_default=False)
    terms_version = fields.Int(allow_none=True, load_default=None)


class TermsAcceptanceBookingSchema(Schema):
    booking_reference = fields.Str(dump_only=True, allow_none=True)
    customer_first_name = fields.Str(dump_only=True)
    service_name = fields.Str(dump_only=True, allow_none=True)
    add_ons = fields.List(fields.Nested(AppliedAddOnSchema), dump_only=True)
    preferred_date = fields.Date(dump_only=True)
    preferred_time = fields.Time(dump_only=True, allow_none=True)
    duration_minutes = fields.Int(dump_only=True, allow_none=True)
    price = fields.Decimal(dump_only=True, as_string=True, allow_none=True)
    vehicle_registration = fields.Str(dump_only=True, allow_none=True)
    # True when accepting continues into paying a deposit.
    deposit_required = fields.Bool(dump_only=True)


class TermsAcceptanceLookupSchema(Schema):
    """What the acceptance page renders for one token."""

    # AWAITING_ACCEPTANCE | ACCEPTED | EXPIRED | CLOSED
    state = fields.Str(dump_only=True)
    status = fields.Str(dump_only=True)
    garage_id = fields.UUID(dump_only=True)
    garage_name = fields.Str(dump_only=True)
    booking = fields.Nested(TermsAcceptanceBookingSchema, dump_only=True)
    # The *current* terms - only while there is still something to accept.
    terms_and_conditions = fields.Str(dump_only=True, allow_none=True)
    terms_version = fields.Int(dump_only=True, allow_none=True)
    hold_expires_at = fields.DateTime(dump_only=True, allow_none=True)


class TermsAcceptedSchema(Schema):
    # PENDING, or AWAITING_PAYMENT for a deposit booking.
    status = fields.Str(dump_only=True)
    booking_reference = fields.Str(dump_only=True, allow_none=True)
    # Deposit bookings only: the public booking page's own recovery
    # capability (DepositAttemptRecovery), so the customer continues straight
    # into the existing checkout.
    recovery_token = fields.Str(dump_only=True, allow_none=True)
