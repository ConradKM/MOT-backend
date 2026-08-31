from marshmallow import Schema, fields, validate

from app.models.appointments.appointment import APPOINTMENT_STATUSES


class AppointmentSchema(Schema):
    id = fields.UUID(dump_only=True)
    garage_id = fields.UUID(dump_only=True)

    employee_id = fields.UUID(required=True)
    customer_id = fields.UUID(required=True)
    vehicle_id = fields.UUID(allow_none=True, load_default=None)

    start_time = fields.DateTime(required=True)
    end_time = fields.DateTime(required=True)

    appointment_type_id = fields.UUID(required=True)
    status = fields.Str(dump_default="BOOKED", validate=validate.OneOf(APPOINTMENT_STATUSES))
    notes = fields.Str(allow_none=True)

    created_at = fields.DateTime(dump_only=True)
    updated_at = fields.DateTime(dump_only=True)


class AppointmentUpdateSchema(Schema):
    employee_id = fields.UUID()
    customer_id = fields.UUID()
    vehicle_id = fields.UUID(allow_none=True)

    start_time = fields.DateTime()
    end_time = fields.DateTime()

    appointment_type_id = fields.UUID()
    status = fields.Str(validate=validate.OneOf(APPOINTMENT_STATUSES))
    notes = fields.Str(allow_none=True)


class AppointmentQueryArgsSchema(Schema):
    date = fields.Date(load_default=None)
    start_date = fields.Date(load_default=None)
    end_date = fields.Date(load_default=None)
    employee_id = fields.UUID(load_default=None)
    customer_id = fields.UUID(load_default=None)
    vehicle_id = fields.UUID(load_default=None)
    status = fields.Str(load_default=None, validate=validate.OneOf(APPOINTMENT_STATUSES))
    appointment_type_id = fields.UUID(load_default=None)
