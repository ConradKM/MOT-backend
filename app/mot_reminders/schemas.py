from marshmallow import Schema, ValidationError, fields, validate, validates_schema

from .defaults import MAX_DAYS_BEFORE, MIN_DAYS_BEFORE


class ReminderStageStateSchema(Schema):
    """One automatic stage's state for a vehicle this MOT cycle."""

    stage = fields.Str(dump_only=True)  # STAGE_1 | STAGE_2 | STAGE_3
    days_before = fields.Int(dump_only=True)
    enabled = fields.Bool(dump_only=True)
    # sent | scheduled | suppressed | disabled | expired
    state = fields.Str(dump_only=True)
    sent_at = fields.DateTime(dump_only=True, allow_none=True)
    scheduled_for = fields.Date(dump_only=True, allow_none=True)


class ReminderHistoryEntrySchema(Schema):
    stage = fields.Str(dump_only=True, allow_none=True)
    trigger = fields.Str(dump_only=True)  # AUTOMATIC | MANUAL
    channel = fields.Str(dump_only=True)
    status = fields.Str(dump_only=True)  # SENT | SKIPPED | FAILED | PENDING
    sent_at = fields.DateTime(dump_only=True, allow_none=True)
    scheduled_at = fields.DateTime(dump_only=True)
    detail = fields.Str(dump_only=True, allow_none=True)
    initiated_by = fields.Str(dump_only=True, allow_none=True)


class MOTReminderRowSchema(Schema):
    """One vehicle's MOT expiry + its full reminder state, for the staff
    MOT Reminders page."""

    vehicle_id = fields.UUID(dump_only=True)
    customer_id = fields.UUID(dump_only=True)
    customer_name = fields.Str(dump_only=True)
    customer_email = fields.Str(dump_only=True, allow_none=True)
    registration_number = fields.Str(dump_only=True)
    make = fields.Str(dump_only=True, allow_none=True)
    model = fields.Str(dump_only=True, allow_none=True)
    mot_expiry_date = fields.Date(dump_only=True)

    # booked | scheduled | sent | expired | not_scheduled
    reminder_status = fields.Str(dump_only=True)
    booking_active = fields.Bool(dump_only=True)
    last_reminder_sent = fields.DateTime(dump_only=True, allow_none=True)
    next_reminder_scheduled = fields.Date(dump_only=True, allow_none=True)
    can_send_manual = fields.Bool(dump_only=True)

    stages = fields.List(fields.Nested(ReminderStageStateSchema), dump_only=True)
    history = fields.List(fields.Nested(ReminderHistoryEntrySchema), dump_only=True)


class ManualReminderSendSchema(Schema):
    """Body for POST /api/mot-reminders/<vehicle_id>/send."""

    # Reserved for future channel selection; email is the only channel today.
    channel = fields.Str(
        load_default=None, validate=validate.OneOf(["email"]), allow_none=True
    )
    # Send even though an MOT booking exists (requires a deliberate confirm).
    acknowledge_booking = fields.Bool(load_default=False)


class ManualReminderResultSchema(Schema):
    stage = fields.Str(dump_only=True)
    trigger = fields.Str(dump_only=True)
    channel = fields.Str(dump_only=True)
    status = fields.Str(dump_only=True)
    sent_at = fields.DateTime(dump_only=True, allow_none=True)
    detail = fields.Str(dump_only=True, allow_none=True)


class MOTReminderSettingsSchema(Schema):
    """Dump + partial update for a garage's MOTReminderSettings row.

    Stages are normalised on save to furthest-out first, so
    stage1_days_before >= stage2_days_before >= stage3_days_before always holds
    for the enabled stages.
    """

    id = fields.UUID(dump_only=True)

    stage1_enabled = fields.Bool()
    stage1_days_before = fields.Int(
        validate=validate.Range(min=MIN_DAYS_BEFORE, max=MAX_DAYS_BEFORE)
    )
    stage2_enabled = fields.Bool()
    stage2_days_before = fields.Int(
        validate=validate.Range(min=MIN_DAYS_BEFORE, max=MAX_DAYS_BEFORE)
    )
    stage3_enabled = fields.Bool()
    stage3_days_before = fields.Int(
        validate=validate.Range(min=MIN_DAYS_BEFORE, max=MAX_DAYS_BEFORE)
    )

    created_at = fields.DateTime(dump_only=True)
    updated_at = fields.DateTime(dump_only=True)

    @validates_schema
    def _no_duplicate_enabled_intervals(self, data, **_kwargs):
        # Only meaningful when the caller sends a full trio; partial updates are
        # re-checked against the merged row in the route.
        trios = [
            ("stage1_enabled", "stage1_days_before"),
            ("stage2_enabled", "stage2_days_before"),
            ("stage3_enabled", "stage3_days_before"),
        ]
        if not all(days in data for _, days in trios):
            return
        enabled_days = [
            data[days]
            for enabled, days in trios
            if data.get(enabled, True)
        ]
        if len(enabled_days) != len(set(enabled_days)):
            raise ValidationError(
                "Enabled reminder stages must use different intervals.",
                field_name="stage2_days_before",
            )
