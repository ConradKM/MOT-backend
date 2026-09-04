from marshmallow import EXCLUDE, Schema, fields, validate


class ScheduleSettingsSchema(Schema):
    """Dump + partial-update for the garage's GarageScheduleSettings row."""

    id = fields.UUID(dump_only=True)
    slot_interval_minutes = fields.Int(validate=validate.Range(min=5, max=240))
    default_appointment_minutes = fields.Int(validate=validate.Range(min=5, max=480))
    min_lead_time_hours = fields.Int(validate=validate.Range(min=0, max=24 * 90))
    max_advance_days = fields.Int(validate=validate.Range(min=1, max=365))
    capacity_per_slot = fields.Int(
        allow_none=True, validate=validate.Range(min=1, max=100)
    )
    limited_threshold_ratio = fields.Float(validate=validate.Range(min=0, max=1))

    created_at = fields.DateTime(dump_only=True)
    updated_at = fields.DateTime(dump_only=True)


class OpeningHoursEntrySchema(Schema):
    class Meta:
        unknown = EXCLUDE

    id = fields.UUID(dump_only=True)
    weekday = fields.Int(required=True, validate=validate.Range(min=0, max=6))
    opens_at = fields.Time(required=True)
    closes_at = fields.Time(required=True)
    is_closed = fields.Bool(load_default=False)


class OpeningHoursReplaceSchema(Schema):
    """PUT body - a full or partial set of weekday rows to upsert."""

    opening_hours = fields.List(
        fields.Nested(OpeningHoursEntrySchema),
        required=True,
        validate=validate.Length(min=1, max=7),
    )


class ScheduleExceptionSchema(Schema):
    class Meta:
        unknown = EXCLUDE

    id = fields.UUID(dump_only=True)
    date = fields.Date(required=True)
    is_closed = fields.Bool(load_default=True)
    opens_at = fields.Time(allow_none=True, load_default=None)
    closes_at = fields.Time(allow_none=True, load_default=None)
    note = fields.Str(
        allow_none=True, load_default=None, validate=validate.Length(max=200)
    )

    created_at = fields.DateTime(dump_only=True)


class GarageScheduleSchema(Schema):
    """Full GET /api/garage/schedule payload."""

    settings = fields.Nested(ScheduleSettingsSchema, dump_only=True)
    opening_hours = fields.List(
        fields.Nested(OpeningHoursEntrySchema), dump_only=True
    )
    exceptions = fields.List(
        fields.Nested(ScheduleExceptionSchema), dump_only=True
    )
