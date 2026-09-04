from marshmallow import Schema, fields, validate


class VehicleSchema(Schema):
    id = fields.UUID(dump_only=True)
    garage_id = fields.UUID(dump_only=True)

    customer_id = fields.UUID(required=True)
    registration_number = fields.Str(
        required=True, validate=validate.Length(min=1, max=20)
    )
    make = fields.Str(allow_none=True, validate=validate.Length(max=100))
    model = fields.Str(allow_none=True, validate=validate.Length(max=100))
    year = fields.Int(allow_none=True)
    current_mileage = fields.Int(allow_none=True, validate=validate.Range(min=0))
    mot_expiry_date = fields.Date(allow_none=True)

    # Archived (soft-deleted) rather than hard-deleted once it has MOT
    # history/appointments - see app/vehicles/routes.py::VehicleResource.delete.
    is_active = fields.Bool(dump_only=True)

    created_at = fields.DateTime(dump_only=True)
    updated_at = fields.DateTime(dump_only=True)


class VehicleUpdateSchema(Schema):
    customer_id = fields.UUID()
    registration_number = fields.Str(validate=validate.Length(min=1, max=20))
    make = fields.Str(allow_none=True, validate=validate.Length(max=100))
    model = fields.Str(allow_none=True, validate=validate.Length(max=100))
    year = fields.Int(allow_none=True)
    current_mileage = fields.Int(allow_none=True, validate=validate.Range(min=0))
    mot_expiry_date = fields.Date(allow_none=True)


class VehicleQueryArgsSchema(Schema):
    registration = fields.Str(load_default=None)
    customer_id = fields.UUID(load_default=None)
    mot_expiry_date = fields.Date(load_default=None)
    # See CustomerQueryArgsSchema.include_inactive - same idea.
    include_inactive = fields.Bool(load_default=False)


class VehicleDeleteResultSchema(Schema):
    archived = fields.Bool(dump_only=True)
    deleted = fields.Bool(dump_only=True)
