from marshmallow import Schema, fields, validate

from app.models.appointments.appointment_type import APPOINTMENT_TYPE_STATUSES


class AppointmentTypeSchema(Schema):
    id = fields.UUID(dump_only=True)
    garage_id = fields.UUID(dump_only=True)

    name = fields.Str(required=True, validate=validate.Length(min=1, max=100))
    description = fields.Str(allow_none=True, validate=validate.Length(max=500))
    base_price = fields.Decimal(allow_none=True, as_string=True, places=2)
    status = fields.Str(load_default="ACTIVE", validate=validate.OneOf(APPOINTMENT_TYPE_STATUSES))

    created_at = fields.DateTime(dump_only=True)
    updated_at = fields.DateTime(dump_only=True)


class AppointmentTypeUpdateSchema(Schema):
    name = fields.Str(validate=validate.Length(min=1, max=100))
    description = fields.Str(allow_none=True, validate=validate.Length(max=500))
    base_price = fields.Decimal(allow_none=True, as_string=True, places=2)
    status = fields.Str(validate=validate.OneOf(APPOINTMENT_TYPE_STATUSES))


class AppointmentTypeQueryArgsSchema(Schema):
    status = fields.Str(load_default=None, validate=validate.OneOf(APPOINTMENT_TYPE_STATUSES))
