from marshmallow import Schema, fields, validate

from app.public_booking.schemas import PublicAppointmentTypeSchema


class GarageSchema(Schema):
    id = fields.UUID(dump_only=True)

    name = fields.Str(dump_only=True)
    # Public, generated at onboarding, immutable. Read-only here for the same
    # reason `id` is: it is not something a garage user changes.
    slug = fields.Str(dump_only=True)
    # Platform-controlled presentation key (see app/garages/layouts.py). NULL
    # is dumped as-is and means "the shared default layout".
    layout_variant = fields.Str(dump_only=True, allow_none=True)
    # Customer-facing business details - read-only to garage users, edited only
    # by the platform (see app/garages/details.py + the onboarding CLI).
    email = fields.Email(dump_only=True, allow_none=True)
    phone = fields.Str(dump_only=True, allow_none=True)
    address = fields.Str(dump_only=True, allow_none=True)
    postcode = fields.Str(dump_only=True, allow_none=True)
    website = fields.Str(dump_only=True, allow_none=True)

    created_at = fields.DateTime(dump_only=True)
    updated_at = fields.DateTime(dump_only=True)


class GarageDetailsUpdateSchema(Schema):
    """Platform-side edit of a tenant's business details (onboarding CLI).

    Not reachable by any garage user - `PATCH /api/garage` is 403 for
    everyone. `slug`, `id` and `layout_variant` are not here on purpose.
    """

    name = fields.Str(validate=validate.Length(min=1, max=200))
    email = fields.Email(allow_none=True)
    phone = fields.Str(allow_none=True, validate=validate.Length(max=40))
    address = fields.Str(allow_none=True, validate=validate.Length(max=500))
    postcode = fields.Str(allow_none=True, validate=validate.Length(max=20))
    website = fields.Str(allow_none=True, validate=validate.Length(max=200))


class PublicGarageSchema(Schema):
    """Garage fields safe to expose without auth, for the public booking flow.

    Mirrors PublicGarageDetailSchema (app/public_booking/schemas.py) - this is
    the equivalent lookup by garage id rather than slug (the /book/:garageId
    entry point), so it needs the same appointment_types the wizard's
    date/type/time step relies on.
    """

    id = fields.UUID(dump_only=True)
    name = fields.Str(dump_only=True)
    slug = fields.Str(dump_only=True)
    appointment_types = fields.Method("_get_appointment_types", dump_only=True)

    def _get_appointment_types(self, garage):
        active = [t for t in garage.appointment_types if t.status == "ACTIVE"]
        return PublicAppointmentTypeSchema(many=True).dump(active)


class _CapacityBucketSchema(Schema):
    # Minutes of scheduling time, not a count of appointment rows - an
    # appointment's actual duration and the garage's employee count both
    # affect these (see app/garages/capacity.py).
    booked_minutes = fields.Int(dump_only=True)
    capacity_minutes = fields.Int(dump_only=True)
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
