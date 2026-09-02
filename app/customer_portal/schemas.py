"""Dump-only schemas for the read-only customer portal.

Deliberately separate from the staff-facing schemas (app/vehicles, app/mot_records,
app/appointments): the customer view exposes a narrower, flattened shape and must not
pick up new fields added for staff by accident.
"""

from marshmallow import Schema, fields


class CustomerProfileSchema(Schema):
    id = fields.UUID(dump_only=True)
    first_name = fields.Str(dump_only=True)
    last_name = fields.Str(dump_only=True)
    email = fields.Email(dump_only=True, allow_none=True)
    phone = fields.Str(dump_only=True, allow_none=True)
    garage_name = fields.Str(dump_only=True)


class CustomerMOTRecordSchema(Schema):
    id = fields.UUID(dump_only=True)
    mot_date = fields.Date(dump_only=True)
    expiry_date = fields.Date(dump_only=True)
    result = fields.Str(dump_only=True)
    notes = fields.Str(dump_only=True, allow_none=True)


class CustomerVehicleSchema(Schema):
    id = fields.UUID(dump_only=True)
    registration_number = fields.Str(dump_only=True)
    make = fields.Str(dump_only=True, allow_none=True)
    model = fields.Str(dump_only=True, allow_none=True)
    year = fields.Int(dump_only=True, allow_none=True)
    current_mileage = fields.Int(dump_only=True, allow_none=True)
    mot_expiry_date = fields.Date(dump_only=True, allow_none=True)
    # Ordered newest-first by the Vehicle.mot_records relationship.
    mot_records = fields.List(fields.Nested(CustomerMOTRecordSchema), dump_only=True)


class CustomerAppointmentSummarySchema(Schema):
    id = fields.UUID(dump_only=True)
    start_time = fields.DateTime(dump_only=True)
    end_time = fields.DateTime(dump_only=True)
    status = fields.Str(dump_only=True)
    notes = fields.Str(dump_only=True, allow_none=True)
    appointment_type_name = fields.Str(dump_only=True)
    vehicle_registration = fields.Str(dump_only=True, allow_none=True)


class CustomerAccountSchema(Schema):
    customer = fields.Nested(CustomerProfileSchema, dump_only=True)
    vehicles = fields.List(fields.Nested(CustomerVehicleSchema), dump_only=True)
    appointments = fields.List(
        fields.Nested(CustomerAppointmentSummarySchema), dump_only=True
    )


class CustomerAppointmentVehicleSchema(Schema):
    registration_number = fields.Str(dump_only=True)
    make = fields.Str(dump_only=True, allow_none=True)
    model = fields.Str(dump_only=True, allow_none=True)
    year = fields.Int(dump_only=True, allow_none=True)


class CustomerAppointmentDetailSchema(Schema):
    id = fields.UUID(dump_only=True)
    start_time = fields.DateTime(dump_only=True)
    end_time = fields.DateTime(dump_only=True)
    status = fields.Str(dump_only=True)
    notes = fields.Str(dump_only=True, allow_none=True)
    appointment_type_name = fields.Str(dump_only=True)
    appointment_type_description = fields.Str(dump_only=True, allow_none=True)
    vehicle = fields.Nested(CustomerAppointmentVehicleSchema, dump_only=True, allow_none=True)
    garage_name = fields.Str(dump_only=True)
