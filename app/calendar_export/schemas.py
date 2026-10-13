from marshmallow import Schema, fields, validate

from app.models.calendar_feed import CALENDAR_FEED_SCOPES

_SCOPE_DOC = (
    "Whose appointments: ME (the signed-in employee), ALL (every worker - OWNER "
    "only) or SELECTED (employee_ids - anyone but yourself is OWNER only)."
)
_TYPES_DOC = "Omit (or null) for every appointment type, including ones added later."


class CalendarExportQueryArgsSchema(Schema):
    scope = fields.Str(
        load_default="ME",
        validate=validate.OneOf(CALENDAR_FEED_SCOPES),
        metadata={"description": _SCOPE_DOC},
    )
    # Repeatable in the query string: ?employee_ids=<id>&employee_ids=<id>
    employee_ids = fields.List(fields.UUID(), load_default=list)
    appointment_type_ids = fields.List(
        fields.UUID(), load_default=None, metadata={"description": _TYPES_DOC}
    )
    # Business-local calendar days, inclusive - the same meaning as
    # GET /api/appointments?start_date=&end_date=.
    start_date = fields.Date(required=True)
    end_date = fields.Date(required=True)


class CalendarFeedCreateSchema(Schema):
    name = fields.Str(required=True, validate=validate.Length(min=1, max=100))
    scope = fields.Str(
        load_default="ME",
        validate=validate.OneOf(CALENDAR_FEED_SCOPES),
        metadata={"description": _SCOPE_DOC},
    )
    employee_ids = fields.List(fields.UUID(), load_default=list)
    appointment_type_ids = fields.List(
        fields.UUID(), allow_none=True, load_default=None, metadata={"description": _TYPES_DOC}
    )
    # A rolling window around today, in the business's timezone.
    days_back = fields.Int(load_default=7, validate=validate.Range(min=0, max=90))
    days_forward = fields.Int(load_default=90, validate=validate.Range(min=1, max=365))


class CalendarFeedSchema(Schema):
    """A feed as listed - never its token or URL, which are only returned by
    create and regenerate (CalendarFeedWithUrlSchema)."""

    id = fields.UUID()
    name = fields.Str()
    scope = fields.Str()
    employee_ids = fields.List(fields.UUID())
    appointment_type_ids = fields.List(fields.UUID(), allow_none=True)
    days_back = fields.Int()
    days_forward = fields.Int()
    created_by_employee_id = fields.UUID()
    created_at = fields.DateTime()
    last_accessed_at = fields.DateTime(allow_none=True)


class CalendarFeedWithUrlSchema(CalendarFeedSchema):
    subscription_url = fields.Str(
        metadata={"description": "https:// URL - shown once; store it now or regenerate later."}
    )
    webcal_url = fields.Str(metadata={"description": "The same URL as webcal:// for Apple."})
