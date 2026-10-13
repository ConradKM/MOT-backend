from marshmallow import Schema, fields, validate

from app.garages.appearance import HEX_COLOR_RE, public_colors
from app.garages.logo import logo_public_url


class GarageSchema(Schema):
    id = fields.UUID(dump_only=True)

    name = fields.Str(dump_only=True)
    # Public, generated at onboarding, immutable. Read-only here for the same
    # reason `id` is: it is not something a garage user changes.
    slug = fields.Str(dump_only=True)
    # Platform-controlled presentation key (see app/garages/layouts.py). NULL
    # is dumped as-is and means "the shared default layout".
    layout_variant = fields.Str(dump_only=True, allow_none=True)
    # Customer-facing business details - read-only to garage users, edited only
    # by the platform (see app/garages/details.py + the onboarding CLI).
    email = fields.Email(dump_only=True, allow_none=True)
    phone = fields.Str(dump_only=True, allow_none=True)
    address = fields.Str(dump_only=True, allow_none=True)
    postcode = fields.Str(dump_only=True, allow_none=True)
    website = fields.Str(dump_only=True, allow_none=True)
    # Read-only here too: the logo is managed from Platform Admin
    # (app/platform_admin/routes/tenants.py), same as every other field on
    # this schema - this just lets the garage's own app (its header) show
    # what Platform Admin set. A fresh presigned url each request, or null.
    logo_url = fields.Method("_get_logo_url", dump_only=True)
    # The IANA zone the business's wall-clock times are in - platform-managed,
    # read-only here. The bulk import's AI prompt names it.
    timezone = fields.Str(dump_only=True)
    auto_accept_booking_requests = fields.Bool(dump_only=True)
    auto_accept_booking_requests_enabled = fields.Bool(dump_only=True)
    # The owner's booking-page colours (app/garages/appearance.py), passed
    # through the same defensive read as the public payload: a stored pair
    # that no longer validates reads as null (= the default look).
    booking_background_color = fields.Method("_get_booking_background_color", dump_only=True)
    booking_text_color = fields.Method("_get_booking_text_color", dump_only=True)

    created_at = fields.DateTime(dump_only=True)
    updated_at = fields.DateTime(dump_only=True)

    def _get_logo_url(self, obj):
        return logo_public_url(obj)

    def _get_booking_background_color(self, obj):
        return public_colors(obj)[0]

    def _get_booking_text_color(self, obj):
        return public_colors(obj)[1]


class GarageDetailsUpdateSchema(Schema):
    """Platform-side edit of a tenant's business details (onboarding CLI).

    Not reachable by any garage user - `PATCH /api/garage` is 403 for
    everyone. `slug`, `id` and `layout_variant` are not here on purpose.
    """

    name = fields.Str(validate=validate.Length(min=1, max=200))
    email = fields.Email(allow_none=True)
    phone = fields.Str(allow_none=True, validate=validate.Length(max=40))
    address = fields.Str(allow_none=True, validate=validate.Length(max=500))
    postcode = fields.Str(allow_none=True, validate=validate.Length(max=20))
    website = fields.Str(allow_none=True, validate=validate.Length(max=200))


_HEX_COLOR = validate.Regexp(HEX_COLOR_RE, error="Must be a 6-digit hex colour like #1a2b3c.")


class BookingAppearanceSchema(Schema):
    """``PUT /api/garage/booking-appearance``. OWNER only.

    Both keys are required so a request always states the whole pair - the
    pair is what gets validated (both-or-neither, contrast >= 4.5:1; see
    app/garages/appearance.py). ``null`` for both resets to the default look.
    """

    booking_background_color = fields.Str(required=True, allow_none=True, validate=_HEX_COLOR)
    booking_text_color = fields.Str(required=True, allow_none=True, validate=_HEX_COLOR)


class BookingRequestSettingsSchema(Schema):
    auto_accept_booking_requests = fields.Bool(required=True)


class BookingRequestAutoAcceptSchema(Schema):
    auto_accept_booking_requests_enabled = fields.Bool(required=True)


class _CapacityBucketSchema(Schema):
    # Minutes of scheduling time, not a count of appointment rows - an
    # appointment's actual duration and the garage's employee count both
    # affect these (see app/garages/capacity.py).
    booked_minutes = fields.Int(dump_only=True)
    capacity_minutes = fields.Int(dump_only=True)
    level = fields.Str(dump_only=True)  # green | amber | red


class TodayCapacitySchema(_CapacityBucketSchema):
    date = fields.Date(dump_only=True)


class WeekCapacitySchema(_CapacityBucketSchema):
    start = fields.Date(dump_only=True)
    end = fields.Date(dump_only=True)


class CapacitySummarySchema(Schema):
    """Booked vs. configured capacity for the staff dashboard."""

    today = fields.Nested(TodayCapacitySchema, dump_only=True)
    week = fields.Nested(WeekCapacitySchema, dump_only=True)
