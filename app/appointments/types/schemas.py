from marshmallow import Schema, fields, validate

from app.models.appointments.appointment_type import APPOINTMENT_TYPE_STATUSES, DEPOSIT_TYPES


class AppointmentTypeSchema(Schema):
    id = fields.UUID(dump_only=True)
    garage_id = fields.UUID(dump_only=True)

    name = fields.Str(required=True, validate=validate.Length(min=1, max=100))
    description = fields.Str(allow_none=True, validate=validate.Length(max=500))
    base_price = fields.Decimal(allow_none=True, as_string=True, places=2)
    status = fields.Str(load_default="ACTIVE", validate=validate.OneOf(APPOINTMENT_TYPE_STATUSES))
    default_duration_minutes = fields.Int(allow_none=True, validate=validate.Range(min=1))
    # None = ungrouped, the normal state for a short menu. The route checks
    # the group belongs to the same business before assigning it; a UUID from
    # a client is never trusted to be in-tenant just because it parses.
    group_id = fields.UUID(allow_none=True, load_default=None)
    order = fields.Int(load_default=0, validate=validate.Range(min=0))

    image_url = fields.Method("_get_image_url", dump_only=True)
    image_content_type = fields.Str(dump_only=True, allow_none=True)
    image_uploaded_at = fields.DateTime(dump_only=True, allow_none=True)

    # --- deposit configuration (see app/payments/money.py for the actual
    # cross-field rules - enforced in the route, not here, so a PATCH's
    # partial body can be validated against the row's *existing* values too).
    deposit_required = fields.Bool(load_default=False)
    deposit_type = fields.Str(
        allow_none=True, load_default=None, validate=validate.OneOf(DEPOSIT_TYPES)
    )
    deposit_value = fields.Decimal(allow_none=True, load_default=None, as_string=True, places=2)
    deposit_currency = fields.Str(load_default="GBP", validate=validate.Equal("GBP"))

    created_at = fields.DateTime(dump_only=True)
    updated_at = fields.DateTime(dump_only=True)

    def _get_image_url(self, appointment_type):
        from app.appointments.images import owner_image_url

        return owner_image_url(appointment_type)


class AppointmentTypeUpdateSchema(Schema):
    name = fields.Str(validate=validate.Length(min=1, max=100))
    description = fields.Str(allow_none=True, validate=validate.Length(max=500))
    base_price = fields.Decimal(allow_none=True, as_string=True, places=2)
    status = fields.Str(validate=validate.OneOf(APPOINTMENT_TYPE_STATUSES))
    default_duration_minutes = fields.Int(allow_none=True, validate=validate.Range(min=1))
    group_id = fields.UUID(allow_none=True)
    order = fields.Int(validate=validate.Range(min=0))

    deposit_required = fields.Bool()
    deposit_type = fields.Str(allow_none=True, validate=validate.OneOf(DEPOSIT_TYPES))
    deposit_value = fields.Decimal(allow_none=True, as_string=True, places=2)
    deposit_currency = fields.Str(validate=validate.Equal("GBP"))


class AppointmentTypeQueryArgsSchema(Schema):
    status = fields.Str(load_default=None, validate=validate.OneOf(APPOINTMENT_TYPE_STATUSES))
