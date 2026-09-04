from marshmallow import Schema, fields


class MOTReminderRowSchema(Schema):
    """One vehicle's MOT expiry + its reminder state, for the staff
    MOT Reminders visibility page."""

    vehicle_id = fields.UUID(dump_only=True)
    customer_id = fields.UUID(dump_only=True)
    customer_name = fields.Str(dump_only=True)
    registration_number = fields.Str(dump_only=True)
    make = fields.Str(dump_only=True, allow_none=True)
    model = fields.Str(dump_only=True, allow_none=True)
    mot_expiry_date = fields.Date(dump_only=True)
    # scheduled: a future reminder is queued. sent: one has been sent and none
    # is queued. not_scheduled: no MOT reminder exists for this vehicle yet.
    reminder_status = fields.Str(dump_only=True)
    last_reminder_sent = fields.DateTime(dump_only=True, allow_none=True)
    next_reminder_scheduled = fields.DateTime(dump_only=True, allow_none=True)
