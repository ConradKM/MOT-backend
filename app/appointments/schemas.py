from marshmallow import Schema, fields, validate

from app.models.appointment import APPOINTMENT_STATUSES, APPOINTMENT_TYPES


class AppointmentSchema(Schema):
    id = fields.Int(dump_only=True)
    garage_id = fields.Int(dump_only=True)

    employee_id = fields.Int(required=True)
    customer_id = fields.Int(required=True)
    vehicle_id = fields.Int(allow_none=True, load_default=None)

    start_time = fields.DateTime(required=True)
    end_time = fields.DateTime(required=True)

    appointment_type = fields.Str(required=True, validate=validate.OneOf(APPOINTMENT_TYPES))
    status = fields.Str(dump_default="BOOKED", validate=validate.OneOf(APPOINTMENT_STATUSES))
    notes = fields.Str(allow_none=True)

    created_at = fields.DateTime(dump_only=True)
    updated_at = fields.DateTime(dump_only=True)


class AppointmentUpdateSchema(Schema):
    employee_id = fields.Int()
    customer_id = fields.Int()
    vehicle_id = fields.Int(allow_none=True)

    start_time = fields.DateTime()
    end_time = fields.DateTime()

    appointment_type = fields.Str(validate=validate.OneOf(APPOINTMENT_TYPES))
    status = fields.Str(validate=validate.OneOf(APPOINTMENT_STATUSES))
    notes = fields.Str(allow_none=True)


class AppointmentQueryArgsSchema(Schema):
    date = fields.Date(load_default=None)
    start_date = fields.Date(load_default=None)
    end_date = fields.Date(load_default=None)
    employee_id = fields.Int(load_default=None)
    customer_id = fields.Int(load_default=None)
    vehicle_id = fields.Int(load_default=None)
    status = fields.Str(load_default=None, validate=validate.OneOf(APPOINTMENT_STATUSES))
    appointment_type = fields.Str(load_default=None, validate=validate.OneOf(APPOINTMENT_TYPES))
