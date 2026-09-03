from datetime import date

from marshmallow import (
    EXCLUDE,
    Schema,
    ValidationError,
    fields,
    pre_load,
    validate,
    validates,
)


class PublicAppointmentTypeSchema(Schema):
    id = fields.UUID(dump_only=True)
    name = fields.Str(dump_only=True)
    description = fields.Str(dump_only=True, allow_none=True)
    base_price = fields.Decimal(dump_only=True, as_string=True, allow_none=True)
    default_duration_minutes = fields.Int(dump_only=True, allow_none=True)


class PublicGarageDetailSchema(Schema):
    """What a logged-out customer needs to render the booking wizard for one garage."""

    id = fields.UUID(dump_only=True)
    name = fields.Str(dump_only=True)
    slug = fields.Str(dump_only=True)
    appointment_types = fields.List(
        fields.Nested(PublicAppointmentTypeSchema), dump_only=True
    )


_CURRENT_YEAR = date.today().year


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
    customer_phone = fields.Str(
        allow_none=True, load_default=None, validate=validate.Length(max=40)
    )

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
        if value < date.today():
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
