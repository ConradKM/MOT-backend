from marshmallow import Schema, fields, validate

from app.models.booking_request import BOOKING_REQUEST_STATUSES


class _RequestAppointmentTypeSchema(Schema):
    """Just enough of the appointment type for the review screen to show the
    service, its price and its duration without a second round trip."""

    id = fields.UUID(dump_only=True)
    name = fields.Str(dump_only=True)
    description = fields.Str(dump_only=True, allow_none=True)
    base_price = fields.Decimal(dump_only=True, as_string=True, allow_none=True)
    default_duration_minutes = fields.Int(dump_only=True, allow_none=True)
    status = fields.Str(dump_only=True)


class SlotCheckSchema(Schema):
    """Live re-check of the request's preferred slot, computed at read time -
    the "current availability/conflict status" shown before a decision."""

    checked = fields.Bool(dump_only=True)
    available = fields.Bool(dump_only=True, allow_none=True)
    reason = fields.Str(dump_only=True, allow_none=True)


class BookingRequestSchema(Schema):
    """Full dump of a booking request, for staff review."""

    id = fields.UUID(dump_only=True)
    garage_id = fields.UUID(dump_only=True)
    status = fields.Str(dump_only=True)

    customer_first_name = fields.Str(dump_only=True)
    customer_last_name = fields.Str(dump_only=True)
    customer_full_name = fields.Method("_get_customer_full_name", dump_only=True)
    customer_email = fields.Email(dump_only=True)
    customer_phone = fields.Str(dump_only=True, allow_none=True)

    vehicle_registration = fields.Str(dump_only=True)
    vehicle_make = fields.Str(dump_only=True, allow_none=True)
    vehicle_model = fields.Str(dump_only=True, allow_none=True)
    vehicle_year = fields.Int(dump_only=True, allow_none=True)
    vehicle_mileage = fields.Int(dump_only=True, allow_none=True)

    appointment_type_id = fields.UUID(dump_only=True, allow_none=True)
    # The appointment type as it stands today (name/price/duration/status) -
    # null once the type has been hard-deleted (only possible if it was never
    # used elsewhere either; see app/appointments/types/routes.py).
    appointment_type = fields.Nested(
        _RequestAppointmentTypeSchema, dump_only=True, allow_none=True
    )
    # Best-known duration for the job *right now*: the type's own current
    # default, falling back to the garage's default appointment length - see
    # service.py. Informational only; the actual appointment's end_time is
    # set at approval time using the same live lookup.
    duration_minutes = fields.Int(
        dump_only=True, allow_none=True, attribute="_duration_minutes"
    )
    # What the customer actually saw and chose at submission time - a
    # snapshot, so it stays accurate even if the type's own duration/price is
    # edited (or the type is deleted) afterwards. Null on requests submitted
    # before this column existed, or with no type chosen.
    requested_duration_minutes = fields.Int(dump_only=True, allow_none=True)
    requested_price = fields.Decimal(dump_only=True, as_string=True, allow_none=True)

    preferred_date = fields.Date(dump_only=True)
    preferred_time = fields.Time(dump_only=True, allow_none=True)
    preferred_employee_note = fields.Str(dump_only=True, allow_none=True)
    notes = fields.Str(dump_only=True, allow_none=True)

    # True once the preferred date/time has passed while this was still
    # PENDING - kept in sync with `status` (see service.py), exposed directly
    # so the frontend doesn't need to duplicate the "is it stale" rule.
    is_expired = fields.Method("_get_is_expired", dump_only=True)
    slot_check = fields.Nested(SlotCheckSchema, dump_only=True, attribute="_slot_check")

    reviewed_by_employee_id = fields.UUID(dump_only=True, allow_none=True)
    reviewed_by_name = fields.Str(
        dump_only=True, allow_none=True, attribute="_reviewed_by_name"
    )
    reviewed_at = fields.DateTime(dump_only=True, allow_none=True)
    staff_notes = fields.Str(dump_only=True, allow_none=True)

    customer_id = fields.UUID(dump_only=True, allow_none=True)
    vehicle_id = fields.UUID(dump_only=True, allow_none=True)
    appointment_id = fields.UUID(dump_only=True, allow_none=True)

    created_at = fields.DateTime(dump_only=True)
    updated_at = fields.DateTime(dump_only=True)

    def _get_customer_full_name(self, obj):
        return f"{obj.customer_first_name} {obj.customer_last_name}".strip()

    def _get_is_expired(self, obj):
        return obj.status == "EXPIRED"


class BookingRequestApproveSchema(Schema):
    """Staff-supplied details to place the appointment. All optional - anything
    omitted falls back to what the request itself carried (see routes.py)."""

    employee_id = fields.UUID(allow_none=True, load_default=None)
    start_time = fields.DateTime(allow_none=True, load_default=None)
    end_time = fields.DateTime(allow_none=True, load_default=None)
    appointment_type_id = fields.UUID(allow_none=True, load_default=None)
    staff_notes = fields.Str(allow_none=True, load_default=None)


class BookingRequestRejectSchema(Schema):
    staff_notes = fields.Str(allow_none=True, load_default=None)


class BookingRequestQueryArgsSchema(Schema):
    status = fields.Str(
        load_default=None, validate=validate.OneOf(BOOKING_REQUEST_STATUSES)
    )
