from marshmallow import Schema, fields, validate

from app.models.appointments.appointment_type_group import DISPLAY_MODES


class AppointmentTypeGroupSchema(Schema):
    id = fields.UUID(dump_only=True)
    garage_id = fields.UUID(dump_only=True)

    name = fields.Str(required=True, validate=validate.Length(min=1, max=100))
    description = fields.Str(allow_none=True, validate=validate.Length(max=500))
    order = fields.Int(load_default=0, validate=validate.Range(min=0))
    # None is meaningful and is the default: inherit the business-wide
    # Garage.booking_display_mode rather than freezing a copy of it.
    display_mode = fields.Str(
        allow_none=True, load_default=None, validate=validate.OneOf(DISPLAY_MODES)
    )

    image_url = fields.Method("_get_image_url", dump_only=True)
    image_content_type = fields.Str(dump_only=True, allow_none=True)
    image_uploaded_at = fields.DateTime(dump_only=True, allow_none=True)

    created_at = fields.DateTime(dump_only=True)
    updated_at = fields.DateTime(dump_only=True)

    def _get_image_url(self, group):
        from app.appointments.images import owner_image_url

        return owner_image_url(group)


class AppointmentTypeGroupUpdateSchema(Schema):
    name = fields.Str(validate=validate.Length(min=1, max=100))
    description = fields.Str(allow_none=True, validate=validate.Length(max=500))
    order = fields.Int(validate=validate.Range(min=0))
    display_mode = fields.Str(allow_none=True, validate=validate.OneOf(DISPLAY_MODES))
