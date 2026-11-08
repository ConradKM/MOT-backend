from datetime import timedelta
from functools import partial

from marshmallow import Schema, ValidationError, fields, validate, validates_schema

from app.analytics.queries import MAX_PERIOD_DAYS

from .due_back import DEFAULT_PER_PAGE, MAX_PER_PAGE

#: GBP amounts. Money fields are optional throughout: they are absent (not
#: zero) from a non-owner's payload - see app/insights/service.py.
Money = partial(fields.Decimal, as_string=True, places=2)


class InsightsQueryArgsSchema(Schema):
    # Both default to the last 30 days ending today, in business-local dates
    # (filled in by the route, which knows the business's timezone).
    start = fields.Date(load_default=None)
    end = fields.Date(load_default=None)
    # Owners only: narrow to one employee. Staff are always scoped to
    # themselves; naming anyone else is a 403.
    employee_id = fields.UUID(load_default=None)

    @validates_schema
    def _check_range(self, data, **kwargs):
        start, end = data.get("start"), data.get("end")
        if start is None or end is None:
            if (start is None) != (end is None):
                raise ValidationError("Pass both start and end, or neither.", "start")
            return
        if end < start:
            raise ValidationError("end must be on or after start.", "end")
        if end - start >= timedelta(days=MAX_PERIOD_DAYS):
            raise ValidationError(f"A period can be at most {MAX_PERIOD_DAYS} days.", "end")


class DueBackQueryArgsSchema(Schema):
    page = fields.Int(load_default=1, validate=validate.Range(min=1))
    per_page = fields.Int(
        load_default=DEFAULT_PER_PAGE, validate=validate.Range(min=1, max=MAX_PER_PAGE)
    )
    employee_id = fields.UUID(load_default=None)


class _Lockable(Schema):
    """An advanced section on a plan without ``insights_advanced`` is just
    ``{"locked": true, "feature": "insights_advanced"}``."""

    locked = fields.Bool()
    feature = fields.Str()


class PeriodSchema(Schema):
    start = fields.Date()
    end = fields.Date()
    days = fields.Int()
    granularity = fields.Str()
    timezone = fields.Str()


class ScopeSchema(Schema):
    employee_id = fields.UUID(allow_none=True)
    money_visible = fields.Bool()
    advanced = fields.Bool()


class AppointmentOutcomesSchema(Schema):
    total = fields.Int()
    by_status = fields.Dict(keys=fields.Str(), values=fields.Int())
    completed = fields.Int()
    no_show = fields.Int()
    cancelled = fields.Int()
    upcoming = fields.Int()
    completion_rate = fields.Float(allow_none=True)
    no_show_rate = fields.Float(allow_none=True)
    cancellation_rate = fields.Float(allow_none=True)


class BookingRequestOutcomesSchema(Schema):
    total = fields.Int()
    pending = fields.Int()
    approved = fields.Int()
    rejected = fields.Int()
    expired = fields.Int()
    approval_rate = fields.Float(allow_none=True)
    rejection_rate = fields.Float(allow_none=True)
    expiry_rate = fields.Float(allow_none=True)


class SummarySchema(Schema):
    appointments = fields.Nested(AppointmentOutcomesSchema)
    lost_count = fields.Int()
    customers_seen = fields.Int()
    new_customers = fields.Int()
    returning_customers = fields.Int()
    returning_rate = fields.Float(allow_none=True)
    # Null when scoped to one employee - requests aren't assigned to staff.
    booking_requests = fields.Nested(BookingRequestOutcomesSchema, allow_none=True)
    # Owners only.
    booked_value = Money()
    discounts_given = Money()
    lost_value = Money()
    deposits_collected = Money()
    deposits_count = fields.Int()
    unpriced_appointments = fields.Int()


class TopServiceSchema(Schema):
    appointment_type_id = fields.UUID()
    name = fields.Str()
    appointments = fields.Int()
    completed = fields.Int()
    booked_value = Money()


class AddOnUptakeSchema(Schema):
    name = fields.Str()
    appointments = fields.Int()
    quantity = fields.Int()


class TopServicesSchema(Schema):
    services = fields.List(fields.Nested(TopServiceSchema))
    add_ons = fields.List(fields.Nested(AddOnUptakeSchema))
    appointments_with_add_ons = fields.Int()
    add_on_uptake_rate = fields.Float(allow_none=True)


class TrendPointSchema(Schema):
    bucket_start = fields.Date()
    appointments = fields.Int()
    new_customers = fields.Int()
    booked_value = Money()


class TrendSchema(_Lockable):
    granularity = fields.Str()
    points = fields.List(fields.Nested(TrendPointSchema))


class HeatmapCellSchema(Schema):
    weekday = fields.Int(metadata={"description": "ISO weekday, 1 = Monday."})
    hour = fields.Int()
    appointments = fields.Int()


class HeatmapSchema(_Lockable):
    cells = fields.List(fields.Nested(HeatmapCellSchema))
    max = fields.Int()


class StaffRowSchema(Schema):
    employee_id = fields.UUID()
    name = fields.Str()
    appointments = fields.Int()
    completed = fields.Int()
    no_show = fields.Int()
    cancelled = fields.Int()
    completion_rate = fields.Float(allow_none=True)
    no_show_rate = fields.Float(allow_none=True)
    booked_value = Money()


class StaffSchema(_Lockable):
    rows = fields.List(fields.Nested(StaffRowSchema))


class DueBackItemSchema(Schema):
    customer_id = fields.UUID()
    customer_name = fields.Str()
    appointment_type_id = fields.UUID()
    service_name = fields.Str()
    vehicle_id = fields.UUID(allow_none=True)
    vehicle_registration = fields.Str(allow_none=True)
    last_visit = fields.Date()
    due_date = fields.Date()
    status = fields.Str(metadata={"description": "due | due_soon"})
    basis = fields.Str(metadata={"description": "interval | mot_expiry"})


class DueBackPageSchema(Schema):
    as_of = fields.Date()
    due_soon_days = fields.Int()
    due = fields.Int()
    due_soon = fields.Int()
    total = fields.Int()
    page = fields.Int()
    per_page = fields.Int()
    items = fields.List(fields.Nested(DueBackItemSchema))


class CustomersSchema(_Lockable):
    new_customers = fields.Int()
    returning_customers = fields.Int()
    returning_rate = fields.Float(allow_none=True)
    due_back = fields.Nested(DueBackPageSchema)


class InsightsSchema(Schema):
    period = fields.Nested(PeriodSchema)
    previous_period = fields.Nested(PeriodSchema)
    scope = fields.Nested(ScopeSchema)
    summary = fields.Nested(SummarySchema)
    previous_summary = fields.Nested(SummarySchema)
    top_services = fields.Nested(TopServicesSchema)
    sources = fields.Dict(
        keys=fields.Str(),
        values=fields.Int(),
        metadata={"description": "Bookings by channel: web, conversation, queue, import, staff."},
    )
    trend = fields.Nested(TrendSchema)
    heatmap = fields.Nested(HeatmapSchema)
    # Null for a staff member's own view: no colleague comparison.
    staff = fields.Nested(StaffSchema, allow_none=True)
    customers = fields.Nested(CustomersSchema)
    export = fields.Nested(_Lockable)
