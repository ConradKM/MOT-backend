from datetime import UTC, datetime

from marshmallow import (
    EXCLUDE,
    Schema,
    ValidationError,
    fields,
    pre_load,
    validate,
    validates,
)

from app.phone import InvalidPhoneNumberError, normalize_uk_mobile


class UKMobileField(fields.Str):
    """A phone-number string, normalised to E.164 on the way in.

    Lets the customer type a mobile number the way they normally would
    (`07123 456789`, `+44 7123 456789`, ...) while what's actually stored is
    always E.164 - the format the future SMS/Twilio integration needs.
    """

    def _deserialize(self, value, attr, data, **kwargs):
        value = super()._deserialize(value, attr, data, **kwargs)
        try:
            return normalize_uk_mobile(value)
        except InvalidPhoneNumberError as exc:
            raise ValidationError(str(exc)) from exc


class PublicIncludedItemSchema(Schema):
    """One customer-visible checklist step - what a customer sees under
    "What's included" for a service, never the full internal working
    checklist (see ChecklistTemplateItem.visible_to_customer)."""

    label = fields.Str(dump_only=True)
    description = fields.Str(dump_only=True, allow_none=True)


class PublicAppointmentTypeSchema(Schema):
    id = fields.UUID(dump_only=True)
    name = fields.Str(dump_only=True)
    description = fields.Str(dump_only=True, allow_none=True)
    base_price = fields.Decimal(dump_only=True, as_string=True, allow_none=True)
    default_duration_minutes = fields.Int(dump_only=True, allow_none=True)
    included_items = fields.Method("_get_included_items", dump_only=True)

    def _get_included_items(self, appointment_type):
        template = appointment_type.checklist_template
        if template is None:
            return []
        visible = sorted(
            (i for i in template.items if i.visible_to_customer), key=lambda i: i.order
        )
        return PublicIncludedItemSchema(many=True).dump(visible)


class PublicGarageDetailSchema(Schema):
    """What a logged-out customer needs to render the booking wizard for one garage."""

    id = fields.UUID(dump_only=True)
    name = fields.Str(dump_only=True)
    slug = fields.Str(dump_only=True)
    appointment_types = fields.List(
        fields.Nested(PublicAppointmentTypeSchema), dump_only=True
    )


_CURRENT_YEAR = datetime.now(UTC).year


class BookingRequestCreateSchema(Schema):
    class Meta:
        # Silently drop anything the client sends that we don't model.
        unknown = EXCLUDE

    customer_first_name = fields.Str(
        required=True, validate=validate.Length(min=1, max=100)
    )
    customer_last_name = fields.Str(
        required=True, validate=validate.Length(min=1, max=100)
    )
    customer_email = fields.Email(required=True, validate=validate.Length(max=320))
    # Required: SMS reachability is the point of collecting it (see
    # app/phone.py). Normalised to E.164 for storage.
    customer_phone = UKMobileField(required=True, validate=validate.Length(max=40))

    vehicle_registration = fields.Str(
        required=True, validate=validate.Length(min=1, max=20)
    )
    vehicle_make = fields.Str(
        allow_none=True, load_default=None, validate=validate.Length(max=100)
    )
    vehicle_model = fields.Str(
        allow_none=True, load_default=None, validate=validate.Length(max=100)
    )
    vehicle_year = fields.Int(
        allow_none=True,
        load_default=None,
        validate=validate.Range(min=1900, max=_CURRENT_YEAR + 1),
    )
    vehicle_mileage = fields.Int(
        allow_none=True, load_default=None, validate=validate.Range(min=0)
    )

    appointment_type_id = fields.UUID(allow_none=True, load_default=None)
    preferred_date = fields.Date(required=True)
    preferred_time = fields.Time(allow_none=True, load_default=None)
    preferred_employee_note = fields.Str(
        allow_none=True, load_default=None, validate=validate.Length(max=200)
    )
    notes = fields.Str(
        allow_none=True, load_default=None, validate=validate.Length(max=2000)
    )

    # Verified in the route (needs app context / config), not here.
    captcha_token = fields.Str(load_default="", load_only=True)

    @pre_load
    def _strip_strings(self, data, **kwargs):
        if not isinstance(data, dict):
            return data
        return {
            k: (v.strip() if isinstance(v, str) else v) for k, v in data.items()
        }

    @validates("preferred_date")
    def _not_in_the_past(self, value, **kwargs):
        # `<` not `<=`: today is a valid preferred date (same-day bookings).
        # UTC to match the availability engine (app/public_booking/availability).
        if value < datetime.now(UTC).date():
            raise ValidationError("Preferred date cannot be in the past.")


class BookingRequestCreatedSchema(Schema):
    id = fields.UUID(dump_only=True)
    status = fields.Str(dump_only=True)


# --- Availability calendar -------------------------------------------------


class AvailabilityQueryArgsSchema(Schema):
    class Meta:
        unknown = EXCLUDE

    # `from` is a Python keyword, so bind it to `from_` but keep the query name.
    from_ = fields.Date(data_key="from", load_default=None)
    to = fields.Date(load_default=None)


class DayAvailabilityQueryArgsSchema(Schema):
    class Meta:
        unknown = EXCLUDE

    # Which service the customer is asking about - its duration determines
    # which start times can fit (see app/public_booking/availability.py).
    # Optional: omitted while the customer hasn't chosen a type yet, or for a
    # garage that hasn't configured any.
    appointment_type_id = fields.UUID(load_default=None)


class _GarageRefSchema(Schema):
    slug = fields.Str(dump_only=True)
    name = fields.Str(dump_only=True)


class AvailabilityRulesSchema(Schema):
    slot_interval_minutes = fields.Int(dump_only=True)
    min_lead_time_hours = fields.Int(dump_only=True)
    max_advance_days = fields.Int(dump_only=True)
    booking_window_start = fields.Date(dump_only=True)
    booking_window_end = fields.Date(dump_only=True)


class PublicOpeningHoursEntrySchema(Schema):
    weekday = fields.Int(dump_only=True)
    opens_at = fields.Str(dump_only=True)
    closes_at = fields.Str(dump_only=True)
    is_closed = fields.Bool(dump_only=True)


class DaySummarySchema(Schema):
    date = fields.Date(dump_only=True)
    weekday = fields.Int(dump_only=True)
    is_open = fields.Bool(dump_only=True)
    # available | limited | full | closed | past
    level = fields.Str(dump_only=True)
    open_slots = fields.Int(dump_only=True)
    total_slots = fields.Int(dump_only=True)


class AvailabilityRangeSchema(Schema):
    garage = fields.Nested(_GarageRefSchema, dump_only=True)
    rules = fields.Nested(AvailabilityRulesSchema, dump_only=True)
    opening_hours = fields.List(
        fields.Nested(PublicOpeningHoursEntrySchema), dump_only=True
    )
    days = fields.List(fields.Nested(DaySummarySchema), dump_only=True)


class SlotSchema(Schema):
    start = fields.Str(dump_only=True)  # "HH:MM"
    # available | limited | booked
    status = fields.Str(dump_only=True)
    remaining = fields.Int(dump_only=True)
    capacity = fields.Int(dump_only=True)


class DaySlotsSchema(Schema):
    date = fields.Date(dump_only=True)
    is_open = fields.Bool(dump_only=True)
    level = fields.Str(dump_only=True)
    slots = fields.List(fields.Nested(SlotSchema), dump_only=True)
