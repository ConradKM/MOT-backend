from marshmallow import Schema, fields, validate

from app.models.booking_request import BOOKING_REQUEST_STATUSES


class BookingRequestSchema(Schema):
    """Full dump of a booking request, for staff review."""

    id = fields.UUID(dump_only=True)
    garage_id = fields.UUID(dump_only=True)
    status = fields.Str(dump_only=True)

    customer_first_name = fields.Str(dump_only=True)
    customer_last_name = fields.Str(dump_only=True)
    customer_email = fields.Email(dump_only=True)
    customer_phone = fields.Str(dump_only=True, allow_none=True)

    vehicle_registration = fields.Str(dump_only=True)
    vehicle_make = fields.Str(dump_only=True, allow_none=True)
    vehicle_model = fields.Str(dump_only=True, allow_none=True)
    vehicle_year = fields.Int(dump_only=True, allow_none=True)
    vehicle_mileage = fields.Int(dump_only=True, allow_none=True)

    appointment_type_id = fields.UUID(dump_only=True, allow_none=True)
    preferred_date = fields.Date(dump_only=True)
    preferred_time = fields.Time(dump_only=True, allow_none=True)
    preferred_employee_note = fields.Str(dump_only=True, allow_none=True)
    notes = fields.Str(dump_only=True, allow_none=True)

    reviewed_by_employee_id = fields.UUID(dump_only=True, allow_none=True)
    reviewed_at = fields.DateTime(dump_only=True, allow_none=True)
    staff_notes = fields.Str(dump_only=True, allow_none=True)

    customer_id = fields.UUID(dump_only=True, allow_none=True)
    vehicle_id = fields.UUID(dump_only=True, allow_none=True)
    appointment_id = fields.UUID(dump_only=True, allow_none=True)

    created_at = fields.DateTime(dump_only=True)
    updated_at = fields.DateTime(dump_only=True)


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
