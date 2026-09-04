from marshmallow import Schema, fields, validate


class GarageSchema(Schema):
    id = fields.UUID(dump_only=True)

    name = fields.Str(dump_only=True)
    # Public, generated at onboarding, immutable. Read-only here for the same
    # reason `id` is: it is not something a garage user changes.
    slug = fields.Str(dump_only=True)
    # Platform-controlled presentation key (see app/garages/layouts.py). NULL
    # is dumped as-is and means "the shared default layout".
    layout_variant = fields.Str(dump_only=True, allow_none=True)
    email = fields.Email(dump_only=True)
    phone = fields.Str(dump_only=True)
    address = fields.Str(dump_only=True)

    created_at = fields.DateTime(dump_only=True)
    updated_at = fields.DateTime(dump_only=True)


class GarageUpdateSchema(Schema):
    """Owner-editable *operational* garage fields only.

    `id`, `slug` and `layout_variant` are deliberately absent - they are
    platform-controlled. marshmallow rejects unknown fields, so a PATCH that
    tries to set one is a 422, not a silent no-op.
    """

    name = fields.Str(validate=validate.Length(min=1, max=200))
    email = fields.Email(allow_none=True)
    phone = fields.Str(allow_none=True, validate=validate.Length(max=40))
    address = fields.Str(allow_none=True, validate=validate.Length(max=500))


class PublicGarageSchema(Schema):
    """Garage fields safe to expose without auth, for the public booking flow."""

    id = fields.UUID(dump_only=True)
    name = fields.Str(dump_only=True)
    slug = fields.Str(dump_only=True)


class _CapacityBucketSchema(Schema):
    booked = fields.Int(dump_only=True)
    capacity = fields.Int(dump_only=True)
    level = fields.Str(dump_only=True)  # green | amber | red


class TodayCapacitySchema(_CapacityBucketSchema):
    date = fields.Date(dump_only=True)


class WeekCapacitySchema(_CapacityBucketSchema):
    start = fields.Date(dump_only=True)
    end = fields.Date(dump_only=True)


class CapacitySummarySchema(Schema):
    """Booked vs. configured capacity for the staff dashboard."""

    today = fields.Nested(TodayCapacitySchema, dump_only=True)
    week = fields.Nested(WeekCapacitySchema, dump_only=True)
