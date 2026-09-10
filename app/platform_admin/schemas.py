"""Request/response schemas for ``/api/platform-admin/*``.

Separate from ``app/garages/schemas.py`` on purpose. ``GarageSchema`` is what a
*garage user* may see about their own business; :class:`TenantSchema` here is
what the *platform* sees about a tenant - status, plan, trial end, internal
notes. Keeping them apart is what stops a platform-only field leaking into the
garage API by someone adding it to the wrong schema.
"""

from marshmallow import Schema, fields, validate

from app.models.garage import GARAGE_STATUSES
from app.models.platform.admin import PLATFORM_ADMIN_ROLES

from .features import PLAN_KEYS
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
