from marshmallow import Schema, ValidationError, fields, validate, validates_schema

from app.mot_reminders.schemas import MOTReminderSettingsSchema

from .defaults import MAX_HOURS_BEFORE, MIN_HOURS_BEFORE, VALID_CHANNELS


class AppointmentReminderTimingSchema(Schema):
    id = fields.UUID(dump_only=True)
    hours_before = fields.Int(
        required=True, validate=validate.Range(min=MIN_HOURS_BEFORE, max=MAX_HOURS_BEFORE)
    )
    enabled = fields.Bool(load_default=True)


class AppointmentReminderSettingsSchema(Schema):
    """Dump + full-replace update for a garage's AppointmentReminderSettings.

    ``timings`` is a full replace on PUT (not a merge) - simplest semantics
    for "here are the lead times I want", and avoids ambiguity about how to
    patch one entry out of a list.
    """

    id = fields.UUID(dump_only=True)
    enabled = fields.Bool(load_default=False)
    channels = fields.List(
        fields.Str(validate=validate.OneOf(VALID_CHANNELS)),
        load_default=list,
    )
    available_channels = fields.List(fields.Str(), dump_only=True)
    timings = fields.List(fields.Nested(AppointmentReminderTimingSchema), required=True)

    created_at = fields.DateTime(dump_only=True)
    updated_at = fields.DateTime(dump_only=True)

    @validates_schema
    def _no_duplicate_timings(self, data, **_kwargs):
        timings = data.get("timings")
        if not timings:
            return
        enabled_hours = [t["hours_before"] for t in timings if t.get("enabled", True)]
        if len(enabled_hours) != len(set(enabled_hours)):
            raise ValidationError(
                "Enabled reminder timings must use different lead times.",
                field_name="timings",
            )


class RemindersSettingsSchema(Schema):
    """Aggregate read of every reminder type's settings for the owner-facing
    "Reminders" settings page."""

    mot = fields.Nested(MOTReminderSettingsSchema, dump_only=True)
    appointment = fields.Nested(AppointmentReminderSettingsSchema, dump_only=True)
