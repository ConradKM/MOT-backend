from marshmallow import Schema, ValidationError, fields, validate, validates_schema


class CommunicationCustomerSchema(Schema):
    id = fields.UUID(dump_only=True)
    first_name = fields.Str(dump_only=True)
    last_name = fields.Str(dump_only=True)
    phone = fields.Str(dump_only=True, allow_none=True)


class CommunicationAppointmentSchema(Schema):
    id = fields.UUID(dump_only=True)
    start_time = fields.DateTime(dump_only=True)
    status = fields.Str(dump_only=True)


class CommunicationBookingRequestSchema(Schema):
    id = fields.UUID(dump_only=True)
    status = fields.Str(dump_only=True)


class CommunicationLogSchema(Schema):
    """Full serialization of one CommunicationLog row.

    ``external_id`` (the Twilio SID) is included but is administrator/
    debugging information - the frontend keeps it out of the normal
    workflow (e.g. behind a collapsed "technical details" disclosure)
    rather than hiding it from the API response entirely.
    """

    id = fields.UUID(dump_only=True)
    channel = fields.Str(dump_only=True)
    direction = fields.Str(dump_only=True)
    external_provider = fields.Str(dump_only=True)
    external_id = fields.Str(dump_only=True, allow_none=True)
    from_address = fields.Str(dump_only=True, allow_none=True)
    to_address = fields.Str(dump_only=True, allow_none=True)
    status = fields.Str(dump_only=True)
    trigger_event = fields.Str(dump_only=True, allow_none=True)
    body = fields.Str(dump_only=True, allow_none=True)
    call_duration_seconds = fields.Int(dump_only=True, allow_none=True)
    error_code = fields.Str(dump_only=True, allow_none=True)
    error_message = fields.Str(dump_only=True, allow_none=True)
    read_at = fields.DateTime(dump_only=True, allow_none=True)
    created_at = fields.DateTime(dump_only=True)
    updated_at = fields.DateTime(dump_only=True)

    customer = fields.Nested(CommunicationCustomerSchema, dump_only=True, allow_none=True)
    appointment = fields.Nested(CommunicationAppointmentSchema, dump_only=True, allow_none=True)
    booking_request = fields.Nested(CommunicationBookingRequestSchema, dump_only=True, allow_none=True)


class CallListQueryArgsSchema(Schema):
    direction = fields.Str(load_default=None, validate=validate.OneOf(["INBOUND", "OUTBOUND"]))
    missed_only = fields.Bool(load_default=False)
    search = fields.Str(load_default=None)
    limit = fields.Int(load_default=None, validate=validate.Range(min=1, max=200))
    offset = fields.Int(load_default=0, validate=validate.Range(min=0))


class CallListResponseSchema(Schema):
    items = fields.List(fields.Nested(CommunicationLogSchema), dump_only=True)
    total = fields.Int(dump_only=True)


class ConversationListQueryArgsSchema(Schema):
    search = fields.Str(load_default=None)
    limit = fields.Int(load_default=None, validate=validate.Range(min=1, max=200))
    offset = fields.Int(load_default=0, validate=validate.Range(min=0))


class ConversationSchema(Schema):
    phone = fields.Str(dump_only=True)
    customer = fields.Nested(CommunicationCustomerSchema, dump_only=True, allow_none=True)
    last_message = fields.Nested(CommunicationLogSchema, dump_only=True)
    unread_count = fields.Int(dump_only=True)


class ConversationListResponseSchema(Schema):
    items = fields.List(fields.Nested(ConversationSchema), dump_only=True)
    total = fields.Int(dump_only=True)


class ConversationMessagesQueryArgsSchema(Schema):
    limit = fields.Int(load_default=None, validate=validate.Range(min=1, max=200))


class ConversationMessagesResponseSchema(Schema):
    phone = fields.Str(dump_only=True)
    customer = fields.Nested(CommunicationCustomerSchema, dump_only=True, allow_none=True)
    messages = fields.List(fields.Nested(CommunicationLogSchema), dump_only=True)


class MarkReadResultSchema(Schema):
    updated = fields.Int(dump_only=True)


class SendWhatsAppSchema(Schema):
    """Either `customer_id` (resolves their phone) or a raw `to` number -
    Contact Customer always sends the former; replying in an existing
    conversation with an unmatched number sends the latter."""

    customer_id = fields.UUID(load_default=None, allow_none=True)
    to = fields.Str(load_default=None, allow_none=True, validate=validate.Length(max=40))
    body = fields.Str(required=True, validate=validate.Length(min=1, max=1600))

    @validates_schema
    def _require_target(self, data, **kwargs):
        if not data.get("customer_id") and not data.get("to"):
            raise ValidationError("Provide either customer_id or to.")


class InitiateCallSchema(Schema):
    customer_id = fields.UUID(load_default=None, allow_none=True)
    to = fields.Str(load_default=None, allow_none=True, validate=validate.Length(max=40))

    @validates_schema
    def _require_target(self, data, **kwargs):
        if not data.get("customer_id") and not data.get("to"):
            raise ValidationError("Provide either customer_id or to.")


class OverviewCapabilitiesSchema(Schema):
    communications_enabled = fields.Bool(dump_only=True)
    voice_number_configured = fields.Bool(dump_only=True)
    whatsapp_configured = fields.Bool(dump_only=True)
    outbound_calling_supported = fields.Bool(dump_only=True)


class OverviewSchema(Schema):
    calls_today = fields.Int(dump_only=True)
    missed_calls_today = fields.Int(dump_only=True)
    whatsapp_unread = fields.Int(dump_only=True)
    outgoing_contacts_today = fields.Int(dump_only=True)
    recent = fields.List(fields.Nested(CommunicationLogSchema), dump_only=True)
    capabilities = fields.Nested(OverviewCapabilitiesSchema, dump_only=True)


class UnreadCountSchema(Schema):
    whatsapp_unread = fields.Int(dump_only=True)


# --------------------------------------------------------------------------
# Communications automation settings (Part 26/40) - never Twilio credentials,
# those stay platform/CLI-only (app/garages/details.py, app/communications/cli.py).
# --------------------------------------------------------------------------


class AutomationSettingsSchema(Schema):
    booking_ack_enabled = fields.Bool()
    booking_confirmation_enabled = fields.Bool()
    reminder_enabled = fields.Bool()
    reminder_hours_before = fields.Int(validate=validate.Range(min=1, max=168))
    missed_call_ack_enabled = fields.Bool()
    conversation_automation_enabled = fields.Bool()


# --------------------------------------------------------------------------
# Message templates (Part 24/34)
# --------------------------------------------------------------------------


class MessageTemplateSchema(Schema):
    key = fields.Str(dump_only=True)
    body = fields.Str(dump_only=True)
    default_body = fields.Str(dump_only=True)
    is_custom = fields.Bool(dump_only=True)


class MessageTemplateListResponseSchema(Schema):
    items = fields.List(fields.Nested(MessageTemplateSchema), dump_only=True)


class UpdateMessageTemplateSchema(Schema):
    body = fields.Str(required=True, validate=validate.Length(min=1, max=1600))


class TemplatePreviewSchema(Schema):
    body = fields.Str(required=True, validate=validate.Length(min=1, max=1600))


class TemplatePreviewResultSchema(Schema):
    preview = fields.Str(dump_only=True)


# --------------------------------------------------------------------------
# Callback requests (Part 21/38)
# --------------------------------------------------------------------------


class CallbackRequestSchema(Schema):
    id = fields.UUID(dump_only=True)
    customer = fields.Nested(CommunicationCustomerSchema, dump_only=True, allow_none=True)
    phone_number = fields.Str(dump_only=True)
    reason = fields.Str(dump_only=True, allow_none=True)
    preferred_time = fields.Str(dump_only=True, allow_none=True)
    status = fields.Str(dump_only=True)
    created_at = fields.DateTime(dump_only=True)


class CallbackRequestListQueryArgsSchema(Schema):
    status = fields.Str(load_default=None, validate=validate.OneOf(["PENDING", "COMPLETED", "CANCELLED"]))
    limit = fields.Int(load_default=None, validate=validate.Range(min=1, max=200))
    offset = fields.Int(load_default=0, validate=validate.Range(min=0))


class CallbackRequestListResponseSchema(Schema):
    items = fields.List(fields.Nested(CallbackRequestSchema), dump_only=True)
    total = fields.Int(dump_only=True)


# --------------------------------------------------------------------------
# Staff conversation takeover / resume automation (Part 20)
# --------------------------------------------------------------------------


class ConversationAutomationStatusSchema(Schema):
    phone = fields.Str(dump_only=True)
    status = fields.Str(dump_only=True, allow_none=True)
    intent = fields.Str(dump_only=True, allow_none=True)
    handoff_reason = fields.Str(dump_only=True, allow_none=True)
