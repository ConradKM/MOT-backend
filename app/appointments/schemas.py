from marshmallow import Schema, fields, validate


class AppointmentSchema(Schema):
    id = fields.UUID(dump_only=True)
    garage_id = fields.UUID(dump_only=True)

    employee_id = fields.UUID(required=True)
    customer_id = fields.UUID(required=True)
    vehicle_id = fields.UUID(allow_none=True, load_default=None)

    start_time = fields.DateTime(required=True)
    # Optional on create: if omitted, derived from the appointment type's
    # default_duration_minutes (422 if the type has none set either).
    end_time = fields.DateTime(allow_none=True, load_default=None)

    appointment_type_id = fields.UUID(required=True)
    # No enum here - the allowed values are the garage's configured status keys
    # (with a built-in default set as fallback); validated in the route.
    status = fields.Str(dump_default="BOOKED", validate=validate.Length(min=1, max=30))
    notes = fields.Str(allow_none=True)
    # Snapshot of the appointment type's price when this was created - stays
    # accurate even if the type's base_price changes later. Null for
    # appointments created before this column existed, or for a type with no
    # price set.
    price_at_booking = fields.Decimal(dump_only=True, as_string=True, allow_none=True)

    created_at = fields.DateTime(dump_only=True)
    updated_at = fields.DateTime(dump_only=True)


class AppointmentUpdateSchema(Schema):
    employee_id = fields.UUID()
    customer_id = fields.UUID()
    vehicle_id = fields.UUID(allow_none=True)

    start_time = fields.DateTime()
    end_time = fields.DateTime()

    appointment_type_id = fields.UUID()
    status = fields.Str(validate=validate.Length(min=1, max=30))
    notes = fields.Str(allow_none=True)


class AppointmentQueryArgsSchema(Schema):
    date = fields.Date(load_default=None)
    start_date = fields.Date(load_default=None)
    end_date = fields.Date(load_default=None)
    employee_id = fields.UUID(load_default=None)
    customer_id = fields.UUID(load_default=None)
    vehicle_id = fields.UUID(load_default=None)
    status = fields.Str(load_default=None, validate=validate.Length(min=1, max=30))
    appointment_type_id = fields.UUID(load_default=None)
