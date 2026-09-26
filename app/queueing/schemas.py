from marshmallow import EXCLUDE, Schema, ValidationError, fields, validate, validates_schema

from app.models.queueing.queue_settings import AVERAGE_MODES
from app.public_booking.schemas import UKMobileField

# ---------------------------------------------------------------------------
# Public (unauthenticated) - customer join / status / cancel
# ---------------------------------------------------------------------------


class QueueJoinSchema(Schema):
    class Meta:
        unknown = EXCLUDE

    customer_first_name = fields.Str(required=True, validate=validate.Length(min=1, max=100))
    customer_last_name = fields.Str(
        allow_none=True, load_default=None, validate=validate.Length(max=100)
    )
    # Required even without SMS opt-in: it's how staff match the walk-in to
    # an existing customer record at check-in, and how they'd call them.
    customer_phone = UKMobileField(required=True, validate=validate.Length(max=40))
    sms_opt_in = fields.Bool(load_default=False)
    vehicle_registration = fields.Str(
        allow_none=True, load_default=None, validate=validate.Length(max=20)
    )
    # Optional - a walk-in often doesn't know what they need yet.
    appointment_type_id = fields.UUID(allow_none=True, load_default=None)
    notes = fields.Str(allow_none=True, load_default=None, validate=validate.Length(max=500))
    captcha_token = fields.Str(allow_none=True, load_default=None, load_only=True)


class QueueTokenSchema(Schema):
    """The customer's bearer token, posted in the body - never in a URL (see
    app/queueing/routes.py)."""

    token = fields.Str(required=True, validate=validate.Length(min=16, max=100))


class PublicQueueInfoSchema(Schema):
    garage_name = fields.Str()
    is_open = fields.Bool()
    accepting_joins = fields.Bool()
    refusal_reason = fields.Str(allow_none=True)
    refusal_message = fields.Str(allow_none=True)
    waiting_count = fields.Int()
    # Where a new walk-in would land right now (default service time).
    estimated_start_at = fields.DateTime(allow_none=True)
    estimated_wait_minutes = fields.Int(allow_none=True)
    opens_at = fields.DateTime(allow_none=True)
    closes_at = fields.DateTime(allow_none=True)


class PublicQueueStatusSchema(Schema):
    garage_name = fields.Str()
    queue_is_open = fields.Bool()
    ticket_number = fields.Int()
    status = fields.Str()
    end_reason = fields.Str(allow_none=True)
    customer_first_name = fields.Str()
    # Only while WAITING: 1-based place in line, and how many are ahead.
    position = fields.Int(allow_none=True)
    people_ahead = fields.Int(allow_none=True)
    estimated_start_at = fields.DateTime(allow_none=True)
    estimated_wait_minutes = fields.Int(allow_none=True)
    # False when the estimate runs past closing - see app/queueing/eta.py.
    fits_today = fields.Bool(allow_none=True)
    called_at = fields.DateTime(allow_none=True)
    call_expires_at = fields.DateTime(allow_none=True)


class QueueJoinedSchema(PublicQueueStatusSchema):
    # Returned exactly once. The client keeps it (URL fragment + storage).
    token = fields.Str()


# ---------------------------------------------------------------------------
# Staff
# ---------------------------------------------------------------------------


class QueueEntrySchema(Schema):
    id = fields.UUID()
    status = fields.Str()
    end_reason = fields.Str(allow_none=True)
    ticket_number = fields.Int()
    customer_first_name = fields.Str()
    customer_last_name = fields.Str(allow_none=True)
    customer_phone = fields.Str()
    sms_opt_in = fields.Bool()
    vehicle_registration = fields.Str(allow_none=True)
    notes = fields.Str(allow_none=True)
    appointment_type_id = fields.UUID(allow_none=True)
    appointment_type_name = fields.Str(allow_none=True)
    service_minutes = fields.Int()
    joined_at = fields.DateTime()
    called_at = fields.DateTime(allow_none=True)
    started_at = fields.DateTime(allow_none=True)
    ended_at = fields.DateTime(allow_none=True)
    appointment_id = fields.UUID(allow_none=True)
    position = fields.Int(allow_none=True)
    estimated_start_at = fields.DateTime(allow_none=True)
    estimated_wait_minutes = fields.Int(allow_none=True)
    fits_today = fields.Bool(allow_none=True)
    call_expires_at = fields.DateTime(allow_none=True)


class TimelineAppointmentSchema(Schema):
    id = fields.UUID()
    start_time = fields.DateTime()
    end_time = fields.DateTime()
    status = fields.Str()
    customer_name = fields.Str()
    appointment_type_name = fields.Str(allow_none=True)
    employee_name = fields.Str(allow_none=True)
    # True for an appointment a queue entry was promoted into.
    is_walk_in = fields.Bool()


class AverageSchema(Schema):
    effective_minutes = fields.Int()
    source = fields.Str()
    auto_minutes = fields.Int(allow_none=True)
    auto_sample_size = fields.Int()


class QueueDashboardSchema(Schema):
    now = fields.DateTime()
    service_date = fields.Date()
    is_open = fields.Bool()
    accepting_joins = fields.Bool()
    refusal_reason = fields.Str(allow_none=True)
    refusal_message = fields.Str(allow_none=True)
    capacity = fields.Int()
    opens_at = fields.DateTime(allow_none=True)
    closes_at = fields.DateTime(allow_none=True)
    no_show_timeout_minutes = fields.Int(allow_none=True)
    average = fields.Nested(AverageSchema)
    new_joiner_estimated_start_at = fields.DateTime(allow_none=True)
    new_joiner_fits_today = fields.Bool()
    entries = fields.List(fields.Nested(QueueEntrySchema))
    appointments = fields.List(fields.Nested(TimelineAppointmentSchema))


class StartServiceSchema(Schema):
    class Meta:
        unknown = EXCLUDE

    employee_id = fields.UUID(allow_none=True, load_default=None)
    appointment_type_id = fields.UUID(allow_none=True, load_default=None)


class QueueReorderSchema(Schema):
    entry_ids = fields.List(fields.UUID(), required=True, validate=validate.Length(max=500))


class QueueSettingsSchema(Schema):
    """PUT partial update + GET dump for GarageQueueSettings.

    Capacity is shown here but not editable here: it is the schedule's
    shared ``capacity_per_slot`` (see app/models/queueing/queue_settings.py)
    and is changed through PUT /api/garage/schedule/settings.
    """

    is_open = fields.Bool()
    average_mode = fields.Str(validate=validate.OneOf(AVERAGE_MODES))
    manual_average_minutes = fields.Int(allow_none=True, validate=validate.Range(min=5, max=480))
    no_show_timeout_minutes = fields.Int(allow_none=True, validate=validate.Range(min=1, max=120))
    default_appointment_type_id = fields.UUID(allow_none=True)

    average = fields.Nested(AverageSchema, dump_only=True)
    capacity = fields.Int(dump_only=True)
    capacity_per_slot = fields.Int(dump_only=True, allow_none=True)


class ReservedWindowSchema(Schema):
    class Meta:
        unknown = EXCLUDE

    id = fields.UUID(dump_only=True)
    # Exactly one of weekday (recurring) / date (one-off).
    weekday = fields.Int(allow_none=True, load_default=None, validate=validate.Range(min=0, max=6))
    date = fields.Date(allow_none=True, load_default=None)
    starts_at = fields.Time(required=True)
    ends_at = fields.Time(required=True)
    reserved_capacity = fields.Int(load_default=1, validate=validate.Range(min=1, max=100))
    note = fields.Str(allow_none=True, load_default=None, validate=validate.Length(max=200))
    created_at = fields.DateTime(dump_only=True)

    @validates_schema
    def _check(self, data, **_kwargs):
        if (data.get("weekday") is None) == (data.get("date") is None):
            raise ValidationError(
                "Set exactly one of weekday (every week) or date (one day only).", "weekday"
            )
        if data["starts_at"] >= data["ends_at"]:
            raise ValidationError("starts_at must be before ends_at.", "ends_at")
