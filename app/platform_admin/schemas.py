"""Request/response schemas for ``/api/platform-admin/*``.

Separate from ``app/garages/schemas.py`` on purpose. ``GarageSchema`` is what a
*garage user* may see about their own business; :class:`TenantSchema` here is
what the *platform* sees about a tenant - status, plan, trial end, internal
notes. Keeping them apart is what stops a platform-only field leaking into the
garage API by someone adding it to the wrong schema.
"""

from marshmallow import Schema, fields, validate

from app.communications.provisioning.states import (
    DISPLAY_STATUSES,
    VOICE_STATUSES,
    WHATSAPP_STATUSES,
)
from app.garages.business_onboarding import BOOKING_SETTING_FIELDS, ONBOARDING_STATUSES
from app.garages.layouts import LAYOUT_VARIANTS
from app.models.appointments.appointment_type import APPOINTMENT_TYPE_STATUSES
from app.models.garage import GARAGE_STATUSES
from app.models.platform.admin import PLATFORM_ADMIN_ROLES

from .features import PLAN_KEYS
from .onboarding import STAGE_KEYS
from .tenants import MAX_PAGE_SIZE, SORT_KEYS


class MessageSchema(Schema):
    message = fields.Str()


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------


class PlatformLoginSchema(Schema):
    email = fields.Email(required=True)
    password = fields.Str(required=True, load_only=True)


class PlatformTokenSchema(Schema):
    access_token = fields.Str()
    refresh_token = fields.Str()


class PlatformAccessTokenSchema(Schema):
    access_token = fields.Str()


class PlatformAdminSchema(Schema):
    id = fields.UUID(dump_only=True)
    email = fields.Email(dump_only=True)
    first_name = fields.Str(dump_only=True, allow_none=True)
    last_name = fields.Str(dump_only=True, allow_none=True)
    display_name = fields.Str(dump_only=True)
    role = fields.Str(dump_only=True, validate=validate.OneOf(PLATFORM_ADMIN_ROLES))
    is_superadmin = fields.Bool(dump_only=True)
    is_active = fields.Bool(dump_only=True)
    last_login_at = fields.DateTime(dump_only=True, allow_none=True)
    created_at = fields.DateTime(dump_only=True)


# --------------------------------------------------------------------------
# Tenants
# --------------------------------------------------------------------------


class TenantSchema(Schema):
    """A tenant as the platform sees it - everything ``GarageSchema`` exposes
    to the garage itself, plus the platform-owned lifecycle fields."""

    id = fields.UUID(dump_only=True)
    name = fields.Str(dump_only=True)
    slug = fields.Str(dump_only=True)
    layout_variant = fields.Str(dump_only=True, allow_none=True)

    email = fields.Email(dump_only=True, allow_none=True)
    phone = fields.Str(dump_only=True, allow_none=True)
    address = fields.Str(dump_only=True, allow_none=True)
    postcode = fields.Str(dump_only=True, allow_none=True)
    website = fields.Str(dump_only=True, allow_none=True)

    status = fields.Str(dump_only=True)
    status_changed_at = fields.DateTime(dump_only=True, allow_none=True)
    suspension_reason = fields.Str(dump_only=True, allow_none=True)
    plan = fields.Str(dump_only=True)
    trial_ends_at = fields.DateTime(dump_only=True, allow_none=True)
    internal_notes = fields.Str(dump_only=True, allow_none=True)

    created_at = fields.DateTime(dump_only=True)
    updated_at = fields.DateTime(dump_only=True)


class OnboardingStepSchema(Schema):
    key = fields.Str(dump_only=True)
    label = fields.Str(dump_only=True)
    description = fields.Str(dump_only=True)
    required = fields.Bool(dump_only=True)
    complete = fields.Bool(dump_only=True)


class OnboardingProgressSchema(Schema):
    steps = fields.List(fields.Nested(OnboardingStepSchema), dump_only=True)
    #: Where the tenant is overall, derived from the same completed steps -
    #: see app/platform_admin/onboarding.py::STAGES.
    stage = fields.Str(dump_only=True, validate=validate.OneOf(STAGE_KEYS))
    stage_label = fields.Str(dump_only=True)
    completed_required = fields.Int(dump_only=True)
    total_required = fields.Int(dump_only=True)
    percent_complete = fields.Int(dump_only=True)
    complete = fields.Bool(dump_only=True)


class TenantSummarySchema(Schema):
    """One row of the tenant list."""

    garage = fields.Nested(TenantSchema, dump_only=True)
    owner_email = fields.Email(dump_only=True, allow_none=True)
    last_activity_at = fields.DateTime(dump_only=True, allow_none=True)
    is_dormant = fields.Bool(dump_only=True)
    customer_count = fields.Int(dump_only=True)
    vehicle_count = fields.Int(dump_only=True)
    appointment_count = fields.Int(dump_only=True)
    pending_booking_requests = fields.Int(dump_only=True)
    employee_count = fields.Int(dump_only=True)
    onboarding = fields.Nested(OnboardingProgressSchema, dump_only=True)


class TenantListSchema(Schema):
    items = fields.List(fields.Nested(TenantSummarySchema), dump_only=True)
    total = fields.Int(dump_only=True)
    page = fields.Int(dump_only=True)
    per_page = fields.Int(dump_only=True)
    pages = fields.Int(dump_only=True)


class TenantListQuerySchema(Schema):
    search = fields.Str(load_default=None)
    status = fields.Str(load_default=None, validate=validate.OneOf(GARAGE_STATUSES))
    plan = fields.Str(load_default=None, validate=validate.OneOf(PLAN_KEYS))
    activity = fields.Str(load_default=None, validate=validate.OneOf(("active", "dormant")))
    sort = fields.Str(load_default="created_at", validate=validate.OneOf(SORT_KEYS))
    order = fields.Str(load_default="desc", validate=validate.OneOf(("asc", "desc")))
    stage = fields.Str(load_default=None, validate=validate.OneOf(STAGE_KEYS))
    page = fields.Int(load_default=1, validate=validate.Range(min=1))
    per_page = fields.Int(load_default=25, validate=validate.Range(min=1, max=MAX_PAGE_SIZE))


class TenantUpdateSchema(Schema):
    """Platform-side tenant configuration.

    Contact details are handed to ``app.garages.details.update_garage_details``
    (the same allowlist the onboarding CLI uses); ``plan`` / ``trial_ends_at`` /
    ``internal_notes`` are platform-only. ``status`` is deliberately absent -
    suspending and reactivating are their own audited operations.
    """

    name = fields.Str(validate=validate.Length(min=1, max=200))
    email = fields.Email(allow_none=True)
    phone = fields.Str(allow_none=True, validate=validate.Length(max=40))
    address = fields.Str(allow_none=True, validate=validate.Length(max=500))
    postcode = fields.Str(allow_none=True, validate=validate.Length(max=20))
    website = fields.Str(allow_none=True, validate=validate.Length(max=200))

    plan = fields.Str(validate=validate.OneOf(PLAN_KEYS))
    trial_ends_at = fields.DateTime(allow_none=True)
    internal_notes = fields.Str(allow_none=True, validate=validate.Length(max=5000))


class TenantSuspendSchema(Schema):
    reason = fields.Str(required=True, validate=validate.Length(min=1, max=2000))


class TenantReactivateSchema(Schema):
    status = fields.Str(load_default="ACTIVE", validate=validate.OneOf(("ACTIVE", "TRIAL")))


# --------------------------------------------------------------------------
# Statistics
# --------------------------------------------------------------------------

_CountMap = fields.Dict(keys=fields.Str(), values=fields.Int(), dump_only=True)


class BookingRequestStatsSchema(Schema):
    total = fields.Int(dump_only=True)
    pending = fields.Int(dump_only=True)
    approved = fields.Int(dump_only=True)
    rejected = fields.Int(dump_only=True)
    expired = fields.Int(dump_only=True)
    # `None` where the denominator is zero - "no data" is not "0%".
    approval_rate = fields.Float(dump_only=True, allow_none=True)
    rejection_rate = fields.Float(dump_only=True, allow_none=True)
    expiry_rate = fields.Float(dump_only=True, allow_none=True)


class AppointmentStatsSchema(Schema):
    total = fields.Int(dump_only=True)
    by_status = _CountMap
    completed = fields.Int(dump_only=True)
    no_show = fields.Int(dump_only=True)
    cancelled = fields.Int(dump_only=True)
    upcoming = fields.Int(dump_only=True)
    completion_rate = fields.Float(dump_only=True, allow_none=True)
    no_show_rate = fields.Float(dump_only=True, allow_none=True)
    cancellation_rate = fields.Float(dump_only=True, allow_none=True)


class CustomerStatsSchema(Schema):
    total = fields.Int(dump_only=True)
    new_in_period = fields.Int(dump_only=True)
    with_appointments = fields.Int(dump_only=True)
    repeat_customers = fields.Int(dump_only=True)
    repeat_rate = fields.Float(dump_only=True, allow_none=True)
    vehicles = fields.Int(dump_only=True)


class ChecklistStatsSchema(Schema):
    started = fields.Int(dump_only=True)
    items = fields.Int(dump_only=True)
    items_completed = fields.Int(dump_only=True)
    fully_completed = fields.Int(dump_only=True)
    item_completion_rate = fields.Float(dump_only=True, allow_none=True)
    checklist_completion_rate = fields.Float(dump_only=True, allow_none=True)


class ReminderStatsSchema(Schema):
    total = fields.Int(dump_only=True)
    sent = fields.Int(dump_only=True)
    failed = fields.Int(dump_only=True)
    skipped = fields.Int(dump_only=True)
    pending = fields.Int(dump_only=True)
    by_stage = _CountMap
    vehicles_reminded = fields.Int(dump_only=True)
    vehicles_booked_after_reminder = fields.Int(dump_only=True)
    conversion_rate = fields.Float(dump_only=True, allow_none=True)
    conversion_window_days = fields.Int(dump_only=True)


class ChannelStatsSchema(Schema):
    total = fields.Int(dump_only=True)
    inbound = fields.Int(dump_only=True)
    outbound = fields.Int(dump_only=True)
    system = fields.Int(dump_only=True)
    delivered = fields.Int(dump_only=True)
    failed = fields.Int(dump_only=True)
    skipped_not_configured = fields.Int(dump_only=True)
    other = fields.Int(dump_only=True)
    delivery_rate = fields.Float(dump_only=True, allow_none=True)
    failure_rate = fields.Float(dump_only=True, allow_none=True)


class CommunicationTotalsSchema(Schema):
    total = fields.Int(dump_only=True)
    delivered = fields.Int(dump_only=True)
    failed = fields.Int(dump_only=True)
    skipped_not_configured = fields.Int(dump_only=True)
    inbound = fields.Int(dump_only=True)
    outbound = fields.Int(dump_only=True)
    delivery_rate = fields.Float(dump_only=True, allow_none=True)
    failure_rate = fields.Float(dump_only=True, allow_none=True)


class CommunicationStatsSchema(Schema):
    by_channel = fields.Dict(
        keys=fields.Str(), values=fields.Nested(ChannelStatsSchema), dump_only=True
    )
    totals = fields.Nested(CommunicationTotalsSchema, dump_only=True)


class PeriodQuerySchema(Schema):
    days = fields.Int(load_default=30, validate=validate.Range(min=1, max=730))


class TenantStatsSchema(Schema):
    garage_id = fields.UUID(dump_only=True)
    period_days = fields.Int(dump_only=True)
    period_start = fields.DateTime(dump_only=True)
    period_end = fields.DateTime(dump_only=True)
    booking_requests = fields.Nested(BookingRequestStatsSchema, dump_only=True)
    appointments = fields.Nested(AppointmentStatsSchema, dump_only=True)
    customers = fields.Nested(CustomerStatsSchema, dump_only=True)
    checklists = fields.Nested(ChecklistStatsSchema, dump_only=True)
    mot_reminders = fields.Nested(ReminderStatsSchema, dump_only=True)
    communications = fields.Nested(CommunicationStatsSchema, dump_only=True)


class TenantDetailSchema(TenantSummarySchema):
    """The tenant detail header - the list row plus nothing extra today, but a
    separate schema so the two can diverge without churning the list."""


class TenantCountsSchema(Schema):
    total = fields.Int(dump_only=True)
    active = fields.Int(dump_only=True)
    trial = fields.Int(dump_only=True)
    suspended = fields.Int(dump_only=True)
    dormant = fields.Int(dump_only=True)
    by_plan = _CountMap
    trials_ending_soon = fields.Int(dump_only=True)


class SignupStatsSchema(Schema):
    in_period = fields.Int(dump_only=True)
    previous_period = fields.Int(dump_only=True)
    change_percent = fields.Float(dump_only=True, allow_none=True)
    all_time = fields.Int(dump_only=True)


class PlatformUsageSchema(Schema):
    booking_requests = fields.Int(dump_only=True)
    appointments = fields.Int(dump_only=True)
    customers = fields.Int(dump_only=True)
    vehicles = fields.Int(dump_only=True)
    customers_total = fields.Int(dump_only=True)
    vehicles_total = fields.Int(dump_only=True)
    appointments_total = fields.Int(dump_only=True)


class PlatformOverviewSchema(Schema):
    period_days = fields.Int(dump_only=True)
    period_start = fields.DateTime(dump_only=True)
    period_end = fields.DateTime(dump_only=True)
    tenants = fields.Nested(TenantCountsSchema, dump_only=True)
    signups = fields.Nested(SignupStatsSchema, dump_only=True)
    usage = fields.Nested(PlatformUsageSchema, dump_only=True)
    communications = fields.Nested(CommunicationStatsSchema, dump_only=True)
    # Always null today - the plan breakdown above is the hook a future
    # subscription/revenue metric hangs off. See app/platform_admin/stats.py.
    revenue = fields.Raw(dump_only=True, allow_none=True)


class GrowthPointSchema(Schema):
    date = fields.Date(dump_only=True)
    signups = fields.Int(dump_only=True)
    cumulative = fields.Int(dump_only=True)


class GrowthSchema(Schema):
    period_days = fields.Int(dump_only=True)
    points = fields.List(fields.Nested(GrowthPointSchema), dump_only=True)


class GrowthQuerySchema(Schema):
    days = fields.Int(load_default=90, validate=validate.Range(min=7, max=730))


# --------------------------------------------------------------------------
# Feature flags
# --------------------------------------------------------------------------


class FeatureFlagSchema(Schema):
    key = fields.Str(dump_only=True)
    label = fields.Str(dump_only=True)
    description = fields.Str(dump_only=True)
    plan_default = fields.Bool(dump_only=True)
    #: null = no override, the plan default applies.
    override = fields.Bool(dump_only=True, allow_none=True)
    enabled = fields.Bool(dump_only=True)
    source = fields.Str(dump_only=True)


class FeatureFlagListSchema(Schema):
    plan = fields.Str(dump_only=True)
    flags = fields.List(fields.Nested(FeatureFlagSchema), dump_only=True)


class FeatureFlagUpdateSchema(Schema):
    key = fields.Str(required=True)
    #: ``null`` clears the override and returns the tenant to its plan default.
    enabled = fields.Bool(required=True, allow_none=True)


# --------------------------------------------------------------------------
# Impersonation
# --------------------------------------------------------------------------


class ImpersonationStartSchema(Schema):
    reason = fields.Str(required=True, validate=validate.Length(min=8, max=2000))
    #: Which staff account to act as. Defaults to the tenant's OWNER.
    employee_id = fields.UUID(load_default=None, allow_none=True)


class ImpersonatedEmployeeSchema(Schema):
    id = fields.UUID(dump_only=True)
    email = fields.Email(dump_only=True)
    first_name = fields.Str(dump_only=True, allow_none=True)
    last_name = fields.Str(dump_only=True, allow_none=True)


class ImpersonationSessionSchema(Schema):
    id = fields.UUID(dump_only=True)
    admin_id = fields.UUID(dump_only=True, allow_none=True)
    admin_email = fields.Email(dump_only=True, allow_none=True)
    garage_id = fields.UUID(dump_only=True)
    employee_id = fields.UUID(dump_only=True)
    reason = fields.Str(dump_only=True)
    expires_at = fields.DateTime(dump_only=True)
    revoked_at = fields.DateTime(dump_only=True, allow_none=True)
    created_at = fields.DateTime(dump_only=True)
    is_active = fields.Function(lambda obj: obj.is_active(), dump_only=True)


class ImpersonationGrantSchema(Schema):
    """What starting an impersonation returns.

    Note what is *not* here: a garage access token. The console receives a
    single-use handoff URL, and the garage app exchanges it
    (``POST /api/auth/impersonation/exchange``) for the real short-lived token.
    """

    session = fields.Nested(ImpersonationSessionSchema, dump_only=True)
    handoff_url = fields.Str(dump_only=True)
    handoff_expires_at = fields.DateTime(dump_only=True)
    expires_at = fields.DateTime(dump_only=True)
    employee = fields.Nested(ImpersonatedEmployeeSchema, dump_only=True)
    garage = fields.Nested(TenantSchema, dump_only=True)


class ImpersonationSessionQuerySchema(Schema):
    garage_id = fields.UUID(load_default=None)
    active_only = fields.Bool(load_default=False)
    limit = fields.Int(load_default=50, validate=validate.Range(min=1, max=200))


# --------------------------------------------------------------------------
# Operations
# --------------------------------------------------------------------------


class EmailLogSchema(Schema):
    id = fields.UUID(dump_only=True)
    garage_id = fields.UUID(dump_only=True)
    garage_name = fields.Function(
        lambda obj: obj.garage.name if obj.garage else None, dump_only=True
    )
    to_address = fields.Str(dump_only=True, allow_none=True)
    from_address = fields.Str(dump_only=True, allow_none=True)
    subject = fields.Str(dump_only=True, allow_none=True)
    trigger_event = fields.Str(dump_only=True, allow_none=True)
    status = fields.Str(dump_only=True)
    error_message = fields.Str(dump_only=True, allow_none=True)
    external_provider = fields.Str(dump_only=True)
    retry_of_id = fields.UUID(dump_only=True, allow_none=True)
    created_at = fields.DateTime(dump_only=True)


class EmailLogListSchema(Schema):
    items = fields.List(fields.Nested(EmailLogSchema), dump_only=True)
    total = fields.Int(dump_only=True)
    page = fields.Int(dump_only=True)
    per_page = fields.Int(dump_only=True)
    pages = fields.Int(dump_only=True)


class EmailLogQuerySchema(Schema):
    status = fields.Str(load_default=None)
    garage_id = fields.UUID(load_default=None)
    search = fields.Str(load_default=None)
    days = fields.Int(load_default=None, validate=validate.Range(min=1, max=730))
    page = fields.Int(load_default=1, validate=validate.Range(min=1))
    per_page = fields.Int(load_default=50, validate=validate.Range(min=1, max=200))


class EmailFailureSummarySchema(Schema):
    period_days = fields.Int(dump_only=True)
    total = fields.Int(dump_only=True)
    sent = fields.Int(dump_only=True)
    failed = fields.Int(dump_only=True)
    failure_rate = fields.Float(dump_only=True, allow_none=True)


class FailedCommunicationSchema(EmailLogSchema):
    channel = fields.Str(dump_only=True)
    direction = fields.Str(dump_only=True)
    error_code = fields.Str(dump_only=True, allow_none=True)


class FailuresByTenantSchema(Schema):
    garage_id = fields.UUID(dump_only=True)
    garage_name = fields.Str(dump_only=True)
    failures = fields.Int(dump_only=True)


class FailuresByErrorSchema(Schema):
    error_code = fields.Str(dump_only=True)
    failures = fields.Int(dump_only=True)


class CommunicationFailuresSchema(Schema):
    period_days = fields.Int(dump_only=True)
    total = fields.Int(dump_only=True)
    by_channel = _CountMap
    by_tenant = fields.List(fields.Nested(FailuresByTenantSchema), dump_only=True)
    by_error_code = fields.List(fields.Nested(FailuresByErrorSchema), dump_only=True)
    recent = fields.List(fields.Nested(FailedCommunicationSchema), dump_only=True)


class JobCheckSchema(Schema):
    key = fields.Str(dump_only=True)
    label = fields.Str(dump_only=True)
    status = fields.Str(dump_only=True)
    detail = fields.Str(dump_only=True)
    count = fields.Int(dump_only=True)
    total = fields.Int(dump_only=True)
    last_run_at = fields.DateTime(dump_only=True, allow_none=True)


class JobHealthSchema(Schema):
    status = fields.Str(dump_only=True)
    checked_at = fields.DateTime(dump_only=True)
    checks = fields.List(fields.Nested(JobCheckSchema), dump_only=True)


class FailuresQuerySchema(Schema):
    days = fields.Int(load_default=7, validate=validate.Range(min=1, max=90))
    limit = fields.Int(load_default=50, validate=validate.Range(min=1, max=200))


# --------------------------------------------------------------------------
# Audit log
# --------------------------------------------------------------------------


class AuditLogSchema(Schema):
    id = fields.UUID(dump_only=True)
    admin_id = fields.UUID(dump_only=True, allow_none=True)
    admin_email = fields.Email(dump_only=True, allow_none=True)
    action = fields.Str(dump_only=True)
    garage_id = fields.UUID(dump_only=True, allow_none=True)
    garage_name = fields.Str(dump_only=True, allow_none=True)
    target_type = fields.Str(dump_only=True, allow_none=True)
    target_id = fields.Str(dump_only=True, allow_none=True)
    summary = fields.Str(dump_only=True, allow_none=True)
    details = fields.Raw(dump_only=True, allow_none=True)
    ip_address = fields.Str(dump_only=True, allow_none=True)
    created_at = fields.DateTime(dump_only=True)


class AuditLogListSchema(Schema):
    items = fields.List(fields.Nested(AuditLogSchema), dump_only=True)
    total = fields.Int(dump_only=True)
    page = fields.Int(dump_only=True)
    per_page = fields.Int(dump_only=True)
    pages = fields.Int(dump_only=True)


class AuditLogQuerySchema(Schema):
    admin_id = fields.UUID(load_default=None)
    garage_id = fields.UUID(load_default=None)
    action = fields.Str(load_default=None)
    search = fields.Str(load_default=None)
    page = fields.Int(load_default=1, validate=validate.Range(min=1))
    per_page = fields.Int(load_default=50, validate=validate.Range(min=1, max=200))


# --------------------------------------------------------------------------
# Onboarding a business (app/platform_admin/provisioning.py)
#
# The request shape mirrors `BusinessSpec` rather than the wizard's steps, so
# the console can reorder or merge steps without an API change. Field-level
# rules live here; the cross-field rules ("TRIAL needs a future end date",
# "opens before closes", "no duplicate service names") live in
# `validate_business_spec`, where the CLI gets them too.
# --------------------------------------------------------------------------


class ServiceInputSchema(Schema):
    """One appointment type, as onboarding collects it."""

    name = fields.Str(required=True, validate=validate.Length(min=1, max=100))
    description = fields.Str(allow_none=True, validate=validate.Length(max=500))
    # A decimal *string* on the wire: money must not make a round trip through
    # a binary float on its way to a Numeric(10, 2) column.
    base_price = fields.Decimal(
        allow_none=True, as_string=True, places=2, validate=validate.Range(min=0)
    )
    default_duration_minutes = fields.Int(
        allow_none=True, validate=validate.Range(min=1, max=24 * 60)
    )
    status = fields.Str(load_default="ACTIVE", validate=validate.OneOf(APPOINTMENT_TYPE_STATUSES))


class ServiceUpdateSchema(Schema):
    """A partial edit of one service. Every field optional; at least one required."""

    name = fields.Str(validate=validate.Length(min=1, max=100))
    description = fields.Str(allow_none=True, validate=validate.Length(max=500))
    base_price = fields.Decimal(
        allow_none=True, as_string=True, places=2, validate=validate.Range(min=0)
    )
    default_duration_minutes = fields.Int(
        allow_none=True, validate=validate.Range(min=1, max=24 * 60)
    )
    status = fields.Str(validate=validate.OneOf(APPOINTMENT_TYPE_STATUSES))


class ServiceSchema(Schema):
    id = fields.UUID(dump_only=True)
    name = fields.Str(dump_only=True)
    description = fields.Str(dump_only=True, allow_none=True)
    base_price = fields.Decimal(dump_only=True, as_string=True, allow_none=True)
    default_duration_minutes = fields.Int(dump_only=True, allow_none=True)
    status = fields.Str(dump_only=True)
    created_at = fields.DateTime(dump_only=True)


class OpeningHoursDaySchema(Schema):
    """One weekday. ``is_closed`` days keep whatever times they carry - the
    availability engine ignores them - so the console can reopen a day without
    the operator retyping its hours."""

    weekday = fields.Int(required=True, validate=validate.Range(min=0, max=6))
    opens_at = fields.Time(allow_none=True, load_default=None)
    closes_at = fields.Time(allow_none=True, load_default=None)
    is_closed = fields.Bool(load_default=False)


class OpeningHoursOutSchema(Schema):
    weekday = fields.Int(dump_only=True)
    opens_at = fields.Time(dump_only=True)
    closes_at = fields.Time(dump_only=True)
    is_closed = fields.Bool(dump_only=True)


class TenantOpeningHoursReplaceSchema(Schema):
    opening_hours = fields.List(
        fields.Nested(OpeningHoursDaySchema),
        required=True,
        validate=validate.Length(min=1, max=7),
    )


def _booking_setting_field(key: str):
    kind, low, high = BOOKING_SETTING_FIELDS[key]
    field_type = fields.Float if kind is float else fields.Int
    # capacity_per_slot's null is meaningful: "fall back to the garage's
    # active employee count".
    return field_type(
        allow_none=(key == "capacity_per_slot"),
        validate=validate.Range(min=low, max=high),
    )


#: Built from BOOKING_SETTING_FIELDS so the API surface and the spec validator
#: can never disagree about which settings exist or what range they take.
BookingSettingsSchema = Schema.from_dict(
    {key: _booking_setting_field(key) for key in BOOKING_SETTING_FIELDS},
    name="BookingSettingsSchema",
)


class BookingSettingsOutSchema(Schema):
    slot_interval_minutes = fields.Int(dump_only=True)
    default_appointment_minutes = fields.Int(dump_only=True)
    min_lead_time_hours = fields.Int(dump_only=True)
    max_advance_days = fields.Int(dump_only=True)
    capacity_per_slot = fields.Int(dump_only=True, allow_none=True)
    limited_threshold_ratio = fields.Float(dump_only=True)


class TenantBusinessInputSchema(Schema):
    """Step 1 + step 3: identity and the platform-owned lifecycle.

    The slug is absent by design - it is generated from the name
    (``app/garages/slug.py``) and is immutable, so no client ever supplies one.
    """

    name = fields.Str(required=True, validate=validate.Length(min=1, max=200))
    email = fields.Email(allow_none=True)
    phone = fields.Str(allow_none=True, validate=validate.Length(max=40))
    address = fields.Str(allow_none=True, validate=validate.Length(max=500))
    postcode = fields.Str(allow_none=True, validate=validate.Length(max=20))
    website = fields.Str(allow_none=True, validate=validate.Length(max=200))
    layout_variant = fields.Str(
        allow_none=True, load_default=None, validate=validate.OneOf(sorted(LAYOUT_VARIANTS))
    )

    plan = fields.Str(load_default=None, validate=validate.OneOf(PLAN_KEYS))
    # SUSPENDED is not offerable here - suspending is its own audited operation.
    status = fields.Str(load_default=None, validate=validate.OneOf(ONBOARDING_STATUSES))
    trial_ends_at = fields.DateTime(allow_none=True, load_default=None)
    internal_notes = fields.Str(allow_none=True, validate=validate.Length(max=5000))


class TenantOwnerInputSchema(Schema):
    """Step 2. No password field, on purpose: Platform Admin never chooses,
    sees or stores an owner's password - the owner sets it from an invite."""

    email = fields.Email(required=True)
    first_name = fields.Str(allow_none=True, validate=validate.Length(max=100))
    last_name = fields.Str(allow_none=True, validate=validate.Length(max=100))


class TenantProvisionSchema(Schema):
    business = fields.Nested(TenantBusinessInputSchema, required=True)
    owner = fields.Nested(TenantOwnerInputSchema, required=True)
    services = fields.List(fields.Nested(ServiceInputSchema), load_default=list)
    # Absent (or null) keeps the seeded Mon-Fri 09:00-17:00.
    opening_hours = fields.List(
        fields.Nested(OpeningHoursDaySchema),
        allow_none=True,
        load_default=None,
        validate=validate.Length(min=1, max=7),
    )
    booking_settings = fields.Nested(BookingSettingsSchema, load_default=dict)


class OwnerSchema(Schema):
    id = fields.UUID(dump_only=True)
    email = fields.Email(dump_only=True)
    first_name = fields.Str(dump_only=True, allow_none=True)
    last_name = fields.Str(dump_only=True, allow_none=True)
    created_at = fields.DateTime(dump_only=True)


class OwnerInviteSchema(Schema):
    """Derived from the owner's password-reset token rows - never stored."""

    state = fields.Str(
        dump_only=True, validate=validate.OneOf(("none", "sent", "accepted", "expired"))
    )
    sent_at = fields.DateTime(dump_only=True, allow_none=True)
    expires_at = fields.DateTime(dump_only=True, allow_none=True)
    accepted_at = fields.DateTime(dump_only=True, allow_none=True)


class CommunicationsStatusSchema(Schema):
    """The Onboarding tab's summary of communications.

    A read-only headline, not the workflow: the per-channel states, actions
    and provider errors live on the Communications tab
    (:class:`CommunicationsDetailSchema`). These fields exist so the
    onboarding checklist can say "waiting for Meta" rather than "not done".
    """

    configured = fields.Bool(dump_only=True)
    enabled = fields.Bool(dump_only=True)
    voice_phone_number = fields.Str(dump_only=True, allow_none=True)
    whatsapp_sender = fields.Str(dump_only=True, allow_none=True)
    twilio_subaccount_sid = fields.Str(dump_only=True, allow_none=True)
    voice_status = fields.Str(dump_only=True)
    voice_stage = fields.Str(dump_only=True)
    voice_blocker = fields.Str(dump_only=True, allow_none=True)
    whatsapp_status = fields.Str(dump_only=True)
    whatsapp_stage = fields.Str(dump_only=True)
    whatsapp_blocker = fields.Str(dump_only=True, allow_none=True)


class NextTaskSchema(Schema):
    key = fields.Str(dump_only=True)
    label = fields.Str(dump_only=True)
    description = fields.Str(dump_only=True)
    complete = fields.Bool(dump_only=True)


class TenantConfigurationSchema(Schema):
    """Everything onboarding configured for one tenant."""

    garage = fields.Nested(TenantSchema, dump_only=True)
    owner = fields.Nested(OwnerSchema, dump_only=True, allow_none=True)
    owner_invite = fields.Nested(OwnerInviteSchema, dump_only=True)
    services = fields.List(fields.Nested(ServiceSchema), dump_only=True)
    opening_hours = fields.List(fields.Nested(OpeningHoursOutSchema), dump_only=True)
    booking_settings = fields.Nested(BookingSettingsOutSchema, dump_only=True)
    communications = fields.Nested(CommunicationsStatusSchema, dump_only=True)
    public_booking_url = fields.Str(dump_only=True)
    onboarding = fields.Nested(OnboardingProgressSchema, dump_only=True)


class TenantProvisionResultSchema(TenantConfigurationSchema):
    created = fields.Bool(dump_only=True)
    #: False when the business was created but its invite email failed to send
    #: - the console then offers "resend invite" rather than reporting failure.
    invite_sent = fields.Bool(dump_only=True)
    next_tasks = fields.List(fields.Nested(NextTaskSchema), dump_only=True)


class ServiceDeletedSchema(Schema):
    message = fields.Str(dump_only=True)


class OwnerInviteResultSchema(Schema):
    invite_sent = fields.Bool(dump_only=True)
    owner = fields.Nested(OwnerSchema, dump_only=True)
    owner_invite = fields.Nested(OwnerInviteSchema, dump_only=True)


# --------------------------------------------------------------------------
# Communications setup (app/platform_admin/communications.py)
#
# Dump-only for everything the provider owns, and deliberately narrow on the
# load side: the only secret this API ever *accepts* is a subaccount Auth
# Token being attached (load_only, never echoed back), and the only secret it
# ever handles in passing is Meta's one-time code, which goes straight to
# Twilio and is never persisted. No schema here has a field for
# TWILIO_AUTH_TOKEN, a Meta access token, or a stored OTP, because no
# response is allowed to carry one.
# --------------------------------------------------------------------------


class ProviderErrorSchema(Schema):
    """A provider failure, explained without losing the original."""

    error_code = fields.Str(dump_only=True, allow_none=True)
    error_message = fields.Str(dump_only=True, allow_none=True)
    channel = fields.Str(dump_only=True, allow_none=True)
    meaning = fields.Str(dump_only=True)
    recommended_action = fields.Str(dump_only=True)
    customer_must_act = fields.Bool(dump_only=True)
    customer_explanation = fields.Str(dump_only=True, allow_none=True)
    known = fields.Bool(dump_only=True)


class ChannelErrorSchema(ProviderErrorSchema):
    """One failed communication from ``communication_logs``, explained."""

    id = fields.UUID(dump_only=True)
    direction = fields.Str(dump_only=True)
    status = fields.Str(dump_only=True)
    to_address = fields.Str(dump_only=True, allow_none=True)
    trigger_event = fields.Str(dump_only=True, allow_none=True)
    created_at = fields.DateTime(dump_only=True)


class SetupActionSchema(Schema):
    key = fields.Str(dump_only=True)
    label = fields.Str(dump_only=True)


class PrerequisiteSchema(Schema):
    key = fields.Str(dump_only=True)
    label = fields.Str(dump_only=True)
    satisfied = fields.Bool(dump_only=True)
    how_to_fix = fields.Str(dump_only=True, allow_none=True)


class WebhookUrlsSchema(Schema):
    """The URLs provisioning writes onto Twilio resources - shown so an
    operator can confirm them against the Twilio console by eye."""

    voice_url = fields.Str(dump_only=True)
    status_callback = fields.Str(dump_only=True)
    callback_url = fields.Str(dump_only=True)
    status_callback_url = fields.Str(dump_only=True)


class PlatformReadinessSchema(Schema):
    twilio_configured = fields.Bool(dump_only=True)
    secrets_configured = fields.Bool(dump_only=True)
    webhooks_reachable = fields.Bool(dump_only=True)
    embedded_signup = fields.List(fields.Nested(PrerequisiteSchema), dump_only=True)
    voice_webhooks = fields.Nested(WebhookUrlsSchema, dump_only=True)
    whatsapp_webhooks = fields.Nested(WebhookUrlsSchema, dump_only=True)


class VoiceSetupSchema(Schema):
    status = fields.Str(dump_only=True, validate=validate.OneOf(VOICE_STATUSES))
    status_label = fields.Str(dump_only=True)
    display_status = fields.Str(dump_only=True, validate=validate.OneOf(DISPLAY_STATUSES))
    display_label = fields.Str(dump_only=True)
    blocker = fields.Str(dump_only=True, allow_none=True)
    next_admin_action = fields.Str(dump_only=True, allow_none=True)
    next_customer_action = fields.Str(dump_only=True, allow_none=True)

    phone_number = fields.Str(dump_only=True, allow_none=True)
    number_sid = fields.Str(dump_only=True, allow_none=True)
    capabilities = fields.List(fields.Str(), dump_only=True)
    webhooks_configured = fields.Bool(dump_only=True)
    webhooks_configured_at = fields.DateTime(dump_only=True, allow_none=True)
    escalation_number = fields.Str(dump_only=True, allow_none=True)
    fallback_number = fields.Str(dump_only=True, allow_none=True)
    last_test_call_sid = fields.Str(dump_only=True, allow_none=True)
    last_test_status = fields.Str(dump_only=True, allow_none=True)
    last_test_at = fields.DateTime(dump_only=True, allow_none=True)
    online_at = fields.DateTime(dump_only=True, allow_none=True)
    last_error = fields.Nested(ProviderErrorSchema, dump_only=True, allow_none=True)


class ExistingRegistrationSchema(Schema):
    """Instructions for a number already on WhatsApp. Instructions, not an
    action - CoMaz never deletes a customer's WhatsApp account."""

    phone_number = fields.Str(dump_only=True)
    meaning = fields.Str(dump_only=True)
    steps = fields.List(fields.Str(), dump_only=True)
    comaz_will_not = fields.Str(dump_only=True)


class WhatsAppSetupSchema(Schema):
    status = fields.Str(dump_only=True, validate=validate.OneOf(WHATSAPP_STATUSES))
    status_label = fields.Str(dump_only=True)
    display_status = fields.Str(dump_only=True, validate=validate.OneOf(DISPLAY_STATUSES))
    display_label = fields.Str(dump_only=True)
    blocker = fields.Str(dump_only=True, allow_none=True)
    next_admin_action = fields.Str(dump_only=True, allow_none=True)
    next_customer_action = fields.Str(dump_only=True, allow_none=True)

    phone_number = fields.Str(dump_only=True, allow_none=True)
    sender_address = fields.Str(dump_only=True, allow_none=True)
    number_already_in_use = fields.Bool(dump_only=True)
    waba_id = fields.Str(dump_only=True, allow_none=True)
    meta_business_id = fields.Str(dump_only=True, allow_none=True)
    meta_phone_number_id = fields.Str(dump_only=True, allow_none=True)
    meta_signup_started_at = fields.DateTime(dump_only=True, allow_none=True)
    meta_signup_completed_at = fields.DateTime(dump_only=True, allow_none=True)
    sender_sid = fields.Str(dump_only=True, allow_none=True)
    sender_status = fields.Str(dump_only=True, allow_none=True)
    display_name = fields.Str(dump_only=True, allow_none=True)
    offline_reason = fields.Str(dump_only=True, allow_none=True)
    last_status_check_at = fields.DateTime(dump_only=True, allow_none=True)
    last_test_at = fields.DateTime(dump_only=True, allow_none=True)
    last_test_status = fields.Str(dump_only=True, allow_none=True)
    online_at = fields.DateTime(dump_only=True, allow_none=True)
    last_error = fields.Nested(ProviderErrorSchema, dump_only=True, allow_none=True)
    existing_registration = fields.Nested(
        ExistingRegistrationSchema, dump_only=True, allow_none=True
    )


class CommunicationsSetupSummarySchema(Schema):
    """One business's row in the Communications Setup list."""

    garage_id = fields.UUID(dump_only=True)
    garage_name = fields.Str(dump_only=True)
    garage_slug = fields.Str(dump_only=True)
    communications_enabled = fields.Bool(dump_only=True)
    automation_enabled = fields.Bool(dump_only=True)
    twilio_subaccount_sid = fields.Str(dump_only=True, allow_none=True)
    subaccount_state = fields.Str(dump_only=True)
    voice_phone_number = fields.Str(dump_only=True, allow_none=True)
    voice_status = fields.Str(dump_only=True)
    voice_display_status = fields.Str(dump_only=True)
    whatsapp_number = fields.Str(dump_only=True, allow_none=True)
    whatsapp_status = fields.Str(dump_only=True)
    whatsapp_display_status = fields.Str(dump_only=True)
    waba_id = fields.Str(dump_only=True, allow_none=True)
    sender_status = fields.Str(dump_only=True, allow_none=True)
    webhooks_configured = fields.Bool(dump_only=True)
    display_status = fields.Str(dump_only=True, validate=validate.OneOf(DISPLAY_STATUSES))
    display_label = fields.Str(dump_only=True)
    setup_stage = fields.Str(dump_only=True)
    blocker = fields.Str(dump_only=True, allow_none=True)
    next_admin_action = fields.Str(dump_only=True, allow_none=True)
    next_customer_action = fields.Str(dump_only=True, allow_none=True)
    last_error = fields.Nested(ProviderErrorSchema, dump_only=True, allow_none=True)
    recent_failures = fields.Int(dump_only=True)
    updated_at = fields.DateTime(dump_only=True, allow_none=True)


class CommunicationsOverviewSchema(Schema):
    items = fields.List(fields.Nested(CommunicationsSetupSummarySchema), dump_only=True)
    total = fields.Int(dump_only=True)
    counts_by_status = fields.Dict(keys=fields.Str(), values=fields.Int(), dump_only=True)
    platform = fields.Nested(PlatformReadinessSchema, dump_only=True)


class CommunicationsOverviewQuerySchema(Schema):
    search = fields.Str(load_default=None)
    status = fields.Str(load_default=None, validate=validate.OneOf(DISPLAY_STATUSES))


class CommunicationsDetailSchema(Schema):
    garage_id = fields.UUID(dump_only=True)
    garage_name = fields.Str(dump_only=True)
    garage_slug = fields.Str(dump_only=True)
    communications_enabled = fields.Bool(dump_only=True)
    automation_enabled = fields.Bool(dump_only=True)
    twilio_subaccount_sid = fields.Str(dump_only=True, allow_none=True)
    subaccount_state = fields.Str(dump_only=True)
    messaging_service_sid = fields.Str(dump_only=True, allow_none=True)
    display_status = fields.Str(dump_only=True, validate=validate.OneOf(DISPLAY_STATUSES))
    display_label = fields.Str(dump_only=True)
    voice = fields.Nested(VoiceSetupSchema, dump_only=True)
    whatsapp = fields.Nested(WhatsAppSetupSchema, dump_only=True)
    voice_actions = fields.List(fields.Nested(SetupActionSchema), dump_only=True)
    whatsapp_actions = fields.List(fields.Nested(SetupActionSchema), dump_only=True)
    business_actions = fields.List(fields.Nested(SetupActionSchema), dump_only=True)
    recent_errors = fields.List(fields.Nested(ChannelErrorSchema), dump_only=True)
    notes = fields.Str(dump_only=True, allow_none=True)
    platform = fields.Nested(PlatformReadinessSchema, dump_only=True)
    updated_at = fields.DateTime(dump_only=True, allow_none=True)


class SubaccountAttachSchema(Schema):
    """Attaching a subaccount created by hand in the Twilio console.

    ``auth_token`` is ``load_only`` and is encrypted the moment it arrives
    (``app/communications/secrets.py``). Nothing reads it back out to a
    response, and the audit trail records only the SID.
    """

    subaccount_sid = fields.Str(required=True, validate=validate.Length(min=10, max=64))
    auth_token = fields.Str(required=True, load_only=True, validate=validate.Length(min=10))


class AvailableNumberQuerySchema(Schema):
    country = fields.Str(load_default=None, validate=validate.Length(equal=2))
    area_code = fields.Str(load_default=None, validate=validate.Length(max=6))
    contains = fields.Str(load_default=None, validate=validate.Length(max=20))
    limit = fields.Int(load_default=10, validate=validate.Range(min=1, max=30))


class AvailableNumberSchema(Schema):
    phone_number = fields.Str(dump_only=True)
    friendly_name = fields.Str(dump_only=True, allow_none=True)
    locality = fields.Str(dump_only=True, allow_none=True)
    region = fields.Str(dump_only=True, allow_none=True)
    iso_country = fields.Str(dump_only=True)
    capabilities = fields.List(fields.Str(), dump_only=True)


class AvailableNumberListSchema(Schema):
    items = fields.List(fields.Nested(AvailableNumberSchema), dump_only=True)


#: One E.164 rule for every provisioning endpoint, so they all reject the
#: same shapes rather than each view inventing its own.
_E164 = validate.Regexp(r"^\+[1-9]\d{6,15}$", error="Must be an E.164 number, e.g. +441234567890.")


class VoiceNumberPurchaseSchema(Schema):
    phone_number = fields.Str(required=True, validate=_E164)
    #: True to adopt a number the subaccount already owns instead of buying a
    #: new one - never a silent fallback, because one spends money and the
    #: other does not.
    already_owned = fields.Bool(load_default=False)


class VoiceRoutingSchema(Schema):
    escalation_number = fields.Str(load_default=None, allow_none=True, validate=_E164)
    fallback_number = fields.Str(load_default=None, allow_none=True, validate=_E164)


class TestCallSchema(Schema):
    to_number = fields.Str(required=True, validate=_E164)


class TestCallResultSchema(Schema):
    call_sid = fields.Str(dump_only=True)
    status = fields.Str(dump_only=True)
    to = fields.Str(dump_only=True)
    from_ = fields.Str(dump_only=True, data_key="from")


class WhatsAppNumberSchema(Schema):
    number_e164 = fields.Str(required=True, validate=_E164)
    #: The business told us this number is already on WhatsApp or the WhatsApp
    #: Business App. Routes onboarding to EXISTING_WHATSAPP_MIGRATION_REQUIRED
    #: rather than to signup - CoMaz never clears that registration itself.
    already_on_whatsapp = fields.Bool(load_default=False)


class EmbeddedSignupConfigSchema(Schema):
    """Public Meta identifiers the browser needs to open Embedded Signup.

    Everything here is rendered into Meta's own popup URL by the JS SDK. No
    app secret and no access token appears, and none is needed: the WABA is
    associated server-side through Twilio afterwards.
    """

    ready = fields.Bool(dump_only=True)
    prerequisites = fields.List(fields.Nested(PrerequisiteSchema), dump_only=True)
    app_id = fields.Str(dump_only=True, allow_none=True)
    config_id = fields.Str(dump_only=True, allow_none=True)
    solution_id = fields.Str(dump_only=True, allow_none=True)
    graph_version = fields.Str(dump_only=True)
    state = fields.Str(dump_only=True)


class EmbeddedSignupResultSchema(Schema):
    """What the Meta popup handed back. ``state`` must match the nonce this
    business's launch minted, so one business's result cannot be applied to
    another."""

    state = fields.Str(required=True, validate=validate.Length(min=10, max=64))
    waba_id = fields.Str(required=True, validate=validate.Length(min=3, max=64))
    phone_number_id = fields.Str(
        load_default=None, allow_none=True, validate=validate.Length(max=64)
    )
    business_id = fields.Str(load_default=None, allow_none=True, validate=validate.Length(max=64))


class SenderRegistrationSchema(Schema):
    display_name = fields.Str(required=True, validate=validate.Length(min=2, max=200))
    #: How Meta should deliver the one-time code when the number needs
    #: verifying. Twilio accepts "sms" or "voice".
    verification_method = fields.Str(
        load_default=None, allow_none=True, validate=validate.OneOf(["sms", "voice"])
    )


class VerificationCodeSchema(Schema):
    """Meta's one-time code, passed straight to Twilio.

    ``load_only`` and never persisted: it is a short-lived credential for the
    customer's own number, so there is no state in which CoMaz should be able
    to read one back.
    """

    code = fields.Str(required=True, load_only=True, validate=validate.Length(min=3, max=12))


class TestMessageSchema(Schema):
    to_number = fields.Str(required=True, validate=_E164)


class TestMessageResultSchema(Schema):
    communication_log_id = fields.Str(dump_only=True)
    status = fields.Str(dump_only=True)
    error_code = fields.Str(dump_only=True, allow_none=True)
    error_message = fields.Str(dump_only=True, allow_none=True)


class ToggleSchema(Schema):
    enabled = fields.Bool(required=True)


class ErrorCatalogueSchema(Schema):
    items = fields.List(fields.Nested(ProviderErrorSchema), dump_only=True)
