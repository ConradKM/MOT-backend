from marshmallow import Schema, fields, validate

from app.models.appointments.add_on import ADD_ON_MAX_QUANTITY_LIMIT, ADD_ON_STATUSES

_QUANTITY_RANGE = validate.Range(min=1, max=ADD_ON_MAX_QUANTITY_LIMIT)


class AddOnSchema(Schema):
    id = fields.UUID(dump_only=True)
    garage_id = fields.UUID(dump_only=True)
    appointment_type_id = fields.UUID(dump_only=True)

    name = fields.Str(required=True, validate=validate.Length(min=1, max=100))
    description = fields.Str(allow_none=True, load_default=None, validate=validate.Length(max=500))
    # Signed: an add-on can make an appointment cheaper or shorter as well.
    price_delta = fields.Decimal(load_default="0.00", as_string=True, places=2)
    duration_delta_minutes = fields.Int(load_default=0, validate=validate.Range(-1440, 1440))
    max_quantity = fields.Int(load_default=1, validate=_QUANTITY_RANGE)
    exclusivity_group = fields.Str(
        allow_none=True, load_default=None, validate=validate.Length(max=50)
    )
    status = fields.Str(load_default="ACTIVE", validate=validate.OneOf(ADD_ON_STATUSES))
    order = fields.Int(load_default=0, validate=validate.Range(min=0))

    created_at = fields.DateTime(dump_only=True)
    updated_at = fields.DateTime(dump_only=True)


class AddOnUpdateSchema(Schema):
    name = fields.Str(validate=validate.Length(min=1, max=100))
    description = fields.Str(allow_none=True, validate=validate.Length(max=500))
    price_delta = fields.Decimal(as_string=True, places=2)
    duration_delta_minutes = fields.Int(validate=validate.Range(-1440, 1440))
    max_quantity = fields.Int(validate=_QUANTITY_RANGE)
    exclusivity_group = fields.Str(allow_none=True, validate=validate.Length(max=50))
    status = fields.Str(validate=validate.OneOf(ADD_ON_STATUSES))
    order = fields.Int(validate=validate.Range(min=0))


class AddOnSelectionSchema(Schema):
    """One entry of a booking's requested add-ons."""

    add_on_id = fields.UUID(required=True)
    quantity = fields.Int(load_default=1, validate=_QUANTITY_RANGE)


class AppliedAddOnSchema(Schema):
    """An add-on as snapshotted onto an appointment or booking request."""

    id = fields.UUID(dump_only=True)
    # Null once the catalogue add-on has been deleted - the snapshot remains.
    add_on_id = fields.UUID(dump_only=True, allow_none=True)
    name = fields.Str(dump_only=True)
    quantity = fields.Int(dump_only=True)
    price_delta = fields.Decimal(dump_only=True, as_string=True)
    duration_delta_minutes = fields.Int(dump_only=True)


class PublicAddOnSchema(Schema):
    id = fields.UUID(dump_only=True)
    name = fields.Str(dump_only=True)
    description = fields.Str(dump_only=True, allow_none=True)
    price_delta = fields.Decimal(dump_only=True, as_string=True)
    duration_delta_minutes = fields.Int(dump_only=True)
    max_quantity = fields.Int(dump_only=True)
    exclusivity_group = fields.Str(dump_only=True, allow_none=True)
