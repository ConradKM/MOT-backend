"""Tenant management: list, inspect, configure, suspend, impersonate.

Read is open to any platform admin; every *change* is ``@superadmin_required``
except impersonation, which support needs by definition and which is the most
heavily constrained action in the system anyway (short-lived, revocable,
audited, and unable to reach this namespace - see
``app/platform_admin/impersonation.py``).
"""

import uuid

from flask.views import MethodView
from flask_jwt_extended import jwt_required
from flask_smorest import Blueprint, abort

from app.extensions import db
from app.garages.logo import (
    LogoError,
    LogoNotUploadedError,
    delete_logo,
    finalize_logo_upload,
    logo_metadata,
    request_logo_upload,
)
from app.models.platform.audit_log import (
    ACTION_FEATURE_FLAG_UPDATE,
    ACTION_TENANT_LOGO_DELETE,
    ACTION_TENANT_LOGO_UPLOAD,
)
from app.models.platform.impersonation import ImpersonationSession
from app.platform_admin.audit import record_audit
from app.platform_admin.features import (
    UnknownFeatureError,
    feature_summary,
    set_feature_override,
)
from app.platform_admin.impersonation import (
    ImpersonationError,
    list_impersonation_sessions,
    revoke_impersonation,
    start_impersonation,
)
from app.platform_admin.onboarding import onboarding_progress
from app.platform_admin.provisioning import (
    DuplicateOwnerError,
    ProvisioningError,
    create_service,
    delete_service,
    get_service,
    provision_tenant,
    resend_owner_invite,
    tenant_configuration,
    update_booking_settings,
    update_opening_hours,
    update_service,
)
from app.platform_admin.schemas import (
    BookingSettingsSchema,
    FeatureFlagListSchema,
    FeatureFlagUpdateSchema,
    ImpersonationGrantSchema,
    ImpersonationSessionQuerySchema,
    ImpersonationSessionSchema,
    ImpersonationStartSchema,
    LogoFinalizeSchema,
    LogoResponseSchema,
    LogoUploadRequestSchema,
    LogoUploadTicketSchema,
    OnboardingProgressSchema,
    OwnerInviteResultSchema,
    PeriodQuerySchema,
    ServiceDeletedSchema,
    ServiceInputSchema,
    ServiceSchema,
    ServiceUpdateSchema,
    TenantConfigurationSchema,
    TenantDetailSchema,
    TenantListQuerySchema,
    TenantListSchema,
    TenantOpeningHoursReplaceSchema,
    TenantProvisionResultSchema,
    TenantProvisionSchema,
    TenantReactivateSchema,
    TenantStatsSchema,
    TenantSuspendSchema,
    TenantUpdateSchema,
)
from app.platform_admin.security import (
    get_current_platform_admin,
    platform_admin_required,
    superadmin_required,
)
from app.platform_admin.stats import tenant_stats
from app.platform_admin.tenants import (
    TenantError,
    get_tenant,
    list_tenants,
    reactivate_tenant,
    suspend_tenant,
    tenant_detail,
    update_tenant,
)

platform_tenants_blp = Blueprint(
    "platform_admin_tenants",
    "platform_admin_tenants",
    url_prefix="/api/platform-admin",
    description="Platform Admin: tenant management",
)


def _require_tenant(garage_id: uuid.UUID):
    garage = get_tenant(garage_id)
    if garage is None:
        abort(404, message="Business not found.")
    return garage


def _require_service(garage, service_id: uuid.UUID):
    """A service belonging to *this* tenant. A valid id from another business
    is a 404 here, not somebody else's row."""
    service = get_service(garage, service_id)
    if service is None:
        abort(404, message="Service not found for this business.")
    return service


@platform_tenants_blp.route("/tenants")
class TenantList(MethodView):
    @jwt_required()
    @platform_admin_required
    @platform_tenants_blp.arguments(TenantListQuerySchema, location="query")
    @platform_tenants_blp.response(200, TenantListSchema)
    def get(self, args):
        """Every business on the platform, searchable and filterable.

        Deliberately cross-tenant - this is the operator console. Each row
        carries its signup date, status, onboarding progress and headline
        usage counts, all computed in grouped queries rather than per row.
        """
        try:
            return list_tenants(
                search=args.get("search"),
                status=args.get("status"),
                plan=args.get("plan"),
                activity=args.get("activity"),
                stage=args.get("stage"),
                sort=args.get("sort") or "created_at",
                descending=(args.get("order") or "desc") == "desc",
                page=args.get("page") or 1,
                per_page=args.get("per_page") or 25,
            )
        except TenantError as exc:
            abort(422, message=str(exc))

    @jwt_required()
    @superadmin_required
    @platform_tenants_blp.arguments(TenantProvisionSchema)
    @platform_tenants_blp.response(201, TenantProvisionResultSchema)
    def post(self, data):
        """Onboard a business: the whole tenant, in one transaction.

        Creates the business (with a generated, immutable slug), its default
        appointment statuses, schedule, MOT reminder settings, OWNER/STAFF
        roles and first OWNER login, then its services, opening hours, booking
        settings and plan/status - all through the same
        ``app/garages/business_onboarding.py`` the CLI uses. A failure anywhere
        leaves no tenant at all.

        No password is accepted or returned: the owner receives a single-use
        set-password invite by email. A second submission of the same owner
        email is a 409 - the unique constraint on ``employees.email`` is what
        makes concurrent double-submits safe, not a client-side guard.
        """
        try:
            return provision_tenant(admin=get_current_platform_admin(), data=data), 201
        except DuplicateOwnerError as exc:
            abort(409, message=str(exc))
        except ProvisioningError as exc:
            abort(422, message=str(exc))


@platform_tenants_blp.route("/tenants/<uuid:garage_id>")
class TenantResource(MethodView):
    @jwt_required()
    @platform_admin_required
    @platform_tenants_blp.response(200, TenantDetailSchema)
    def get(self, garage_id):
        """One business: configuration, status, onboarding progress, usage."""
        return tenant_detail(_require_tenant(garage_id))

    @jwt_required()
    @superadmin_required
    @platform_tenants_blp.arguments(TenantUpdateSchema)
    @platform_tenants_blp.response(200, TenantDetailSchema)
    def patch(self, data, garage_id):
        """Edit a business's configuration. Superadmin only, fully audited.

        Contact details go through the same service the onboarding CLI uses;
        ``plan`` / ``trial_ends_at`` / ``internal_notes`` are platform-only.
        The public ``slug`` and internal ``id`` remain immutable, and
        ``status`` is not accepted here - suspension is its own operation.
        """
        garage = _require_tenant(garage_id)
        try:
            update_tenant(admin=get_current_platform_admin(), garage=garage, changes=data)
        except TenantError as exc:
            abort(422, message=str(exc))
        return tenant_detail(garage)


@platform_tenants_blp.route("/tenants/<uuid:garage_id>/suspend")
class TenantSuspend(MethodView):
    @jwt_required()
    @superadmin_required
    @platform_tenants_blp.arguments(TenantSuspendSchema)
    @platform_tenants_blp.response(200, TenantDetailSchema)
    def post(self, data, garage_id):
        """Suspend a business. Staff lose access immediately; nothing is deleted.

        A reason is required and is recorded in the audit trail.
        """
        garage = _require_tenant(garage_id)
        try:
            suspend_tenant(admin=get_current_platform_admin(), garage=garage, reason=data["reason"])
        except TenantError as exc:
            abort(422, message=str(exc))
        return tenant_detail(garage)


@platform_tenants_blp.route("/tenants/<uuid:garage_id>/reactivate")
class TenantReactivate(MethodView):
    @jwt_required()
    @superadmin_required
    @platform_tenants_blp.arguments(TenantReactivateSchema)
    @platform_tenants_blp.response(200, TenantDetailSchema)
    def post(self, data, garage_id):
        """Lift a suspension, back to ACTIVE (or TRIAL)."""
        garage = _require_tenant(garage_id)
        try:
            reactivate_tenant(
                admin=get_current_platform_admin(),
                garage=garage,
                status=data.get("status") or "ACTIVE",
            )
        except TenantError as exc:
            abort(422, message=str(exc))
        return tenant_detail(garage)


@platform_tenants_blp.route("/tenants/<uuid:garage_id>/logo")
class TenantLogo(MethodView):
    """Business branding, kept entirely separate from tenant provisioning -
    see app/garages/logo.py's module docstring for why. A logo upload/replace/
    delete is always its own request against an already-created tenant, never
    part of the ``POST /tenants`` body."""

    @jwt_required()
    @platform_admin_required
    @platform_tenants_blp.response(200, LogoResponseSchema)
    def get(self, garage_id):
        """Current logo metadata + a short-lived download url, or `logo: null`
        if this business has none yet."""
        garage = _require_tenant(garage_id)
        return {"logo": logo_metadata(garage)}

    @jwt_required()
    @superadmin_required
    @platform_tenants_blp.arguments(LogoUploadRequestSchema)
    @platform_tenants_blp.response(201, LogoUploadTicketSchema)
    def post(self, data, garage_id):
        """Issue a presigned upload ticket for a new logo (or a replacement).

        Nothing changes on the business yet - the ticket's `storage_key` only
        becomes the live logo once confirmed via `PUT .../logo/finalize`. A
        client that requests a ticket and never uploads leaves the business
        exactly as it was.
        """
        garage = _require_tenant(garage_id)
        try:
            return (
                request_logo_upload(
                    garage, content_type=data["content_type"], size_bytes=data.get("size_bytes")
                ),
                201,
            )
        except LogoError as exc:
            abort(422, message=str(exc))

    @jwt_required()
    @superadmin_required
    @platform_tenants_blp.response(200, ServiceDeletedSchema)
    def delete(self, garage_id):
        """Remove this business's logo. A no-op (still 200) if it has none."""
        garage = _require_tenant(garage_id)
        had_logo = bool(garage.logo_storage_key)
        delete_logo(garage)
        if had_logo:
            record_audit(
                admin=get_current_platform_admin(),
                action=ACTION_TENANT_LOGO_DELETE,
                garage=garage,
                summary=f"Removed {garage.name}'s logo",
                commit=True,
            )
        return {"message": "Logo removed."}


@platform_tenants_blp.route("/tenants/<uuid:garage_id>/logo/finalize")
class TenantLogoFinalize(MethodView):
    @jwt_required()
    @superadmin_required
    @platform_tenants_blp.arguments(LogoFinalizeSchema)
    @platform_tenants_blp.response(200, LogoResponseSchema)
    def post(self, data, garage_id):
        """Confirm an uploaded object is really an image and make it live.

        Verifies the object actually landed at `storage_key` (409 if not),
        sniffs its real content type from its own bytes rather than trusting
        what was declared when the ticket was issued, and only then persists
        it as this business's logo - deleting the previous one, if any, once
        the new one is confirmed live.
        """
        garage = _require_tenant(garage_id)
        try:
            finalize_logo_upload(
                garage,
                storage_key=data["storage_key"],
                original_filename=data.get("original_filename"),
            )
        except LogoNotUploadedError as exc:
            abort(409, message=str(exc))
        except LogoError as exc:
            abort(422, message=str(exc))

        record_audit(
            admin=get_current_platform_admin(),
            action=ACTION_TENANT_LOGO_UPLOAD,
            garage=garage,
            summary=f"Uploaded a logo for {garage.name}",
            details={"content_type": garage.logo_content_type},
            commit=True,
        )
        return {"logo": logo_metadata(garage)}


@platform_tenants_blp.route("/tenants/<uuid:garage_id>/onboarding")
class TenantOnboarding(MethodView):
    @jwt_required()
    @platform_admin_required
    @platform_tenants_blp.response(200, OnboardingProgressSchema)
    def get(self, garage_id):
        """How far this business has got with setting itself up - derived from
        its own data, never from a stored progress flag."""
        return onboarding_progress(_require_tenant(garage_id))


@platform_tenants_blp.route("/tenants/<uuid:garage_id>/configuration")
class TenantConfiguration(MethodView):
    @jwt_required()
    @platform_admin_required
    @platform_tenants_blp.response(200, TenantConfigurationSchema)
    def get(self, garage_id):
        """Everything onboarding configured for this business.

        Identity, owner and the state of their set-password invite, plan and
        status, services, opening hours, booking settings, communications
        setup, the public booking URL, and the derived onboarding checklist -
        the read behind the console's Onboarding tab.
        """
        return tenant_configuration(_require_tenant(garage_id))


@platform_tenants_blp.route("/tenants/<uuid:garage_id>/services")
class TenantServices(MethodView):
    @jwt_required()
    @superadmin_required
    @platform_tenants_blp.arguments(ServiceInputSchema)
    @platform_tenants_blp.response(201, ServiceSchema)
    def post(self, data, garage_id):
        """Add a service to this business.

        The same ``garage_appointment_types`` row the business creates for
        itself in Settings - there is no onboarding-only service model - so it
        is immediately bookable on the public page.
        """
        garage = _require_tenant(garage_id)
        try:
            return create_service(admin=get_current_platform_admin(), garage=garage, data=data)
        except ProvisioningError as exc:
            abort(422, message=str(exc))


@platform_tenants_blp.route("/tenants/<uuid:garage_id>/services/<uuid:service_id>")
class TenantService(MethodView):
    @jwt_required()
    @superadmin_required
    @platform_tenants_blp.arguments(ServiceUpdateSchema)
    @platform_tenants_blp.response(200, ServiceSchema)
    def patch(self, data, garage_id, service_id):
        """Correct one of this business's services."""
        garage = _require_tenant(garage_id)
        service = _require_service(garage, service_id)
        try:
            return update_service(
                admin=get_current_platform_admin(),
                garage=garage,
                service=service,
                changes=data,
            )
        except ProvisioningError as exc:
            abort(422, message=str(exc))

    @jwt_required()
    @superadmin_required
    @platform_tenants_blp.response(200, ServiceDeletedSchema)
    def delete(self, garage_id, service_id):
        """Remove a service added by mistake.

        Refused once appointments reference it - retire it (``DEPRECATED``)
        instead, so booked history keeps its type.
        """
        garage = _require_tenant(garage_id)
        service = _require_service(garage, service_id)
        try:
            delete_service(admin=get_current_platform_admin(), garage=garage, service=service)
        except ProvisioningError as exc:
            abort(422, message=str(exc))
        return {"message": "Service removed."}


@platform_tenants_blp.route("/tenants/<uuid:garage_id>/opening-hours")
class TenantOpeningHours(MethodView):
    @jwt_required()
    @superadmin_required
    @platform_tenants_blp.arguments(TenantOpeningHoursReplaceSchema)
    @platform_tenants_blp.response(200, TenantConfigurationSchema)
    def put(self, data, garage_id):
        """Set this business's weekday opening hours.

        Writes the same ``garage_opening_hours`` rows Settings > Availability
        writes, so the change is immediately real to the public booking
        calendar, the availability API and the WhatsApp assistant.
        """
        garage = _require_tenant(garage_id)
        try:
            update_opening_hours(
                admin=get_current_platform_admin(),
                garage=garage,
                entries=data["opening_hours"],
            )
        except ProvisioningError as exc:
            abort(422, message=str(exc))
        return tenant_configuration(garage)


@platform_tenants_blp.route("/tenants/<uuid:garage_id>/booking-settings")
class TenantBookingSettings(MethodView):
    @jwt_required()
    @superadmin_required
    @platform_tenants_blp.arguments(BookingSettingsSchema)
    @platform_tenants_blp.response(200, TenantConfigurationSchema)
    def put(self, data, garage_id):
        """Set the booking window and capacity rules for this business.

        Minimum notice, how far ahead customers may book, slot granularity,
        default appointment length and per-slot capacity - the same
        ``garage_schedule_settings`` row the owner edits.
        """
        garage = _require_tenant(garage_id)
        try:
            update_booking_settings(admin=get_current_platform_admin(), garage=garage, changes=data)
        except ProvisioningError as exc:
            abort(422, message=str(exc))
        return tenant_configuration(garage)


@platform_tenants_blp.route("/tenants/<uuid:garage_id>/owner-invite")
class TenantOwnerInvite(MethodView):
    @jwt_required()
    @superadmin_required
    @platform_tenants_blp.response(200, OwnerInviteResultSchema)
    def post(self, garage_id):
        """Send the owner a fresh set-password link.

        For an invite that expired, never arrived, or went to the wrong
        address. Issuing one voids any outstanding link. Platform Admin never
        sees the resulting password.
        """
        garage = _require_tenant(garage_id)
        try:
            return resend_owner_invite(admin=get_current_platform_admin(), garage=garage)
        except ProvisioningError as exc:
            abort(422, message=str(exc))


@platform_tenants_blp.route("/tenants/<uuid:garage_id>/stats")
class TenantStats(MethodView):
    @jwt_required()
    @platform_admin_required
    @platform_tenants_blp.arguments(PeriodQuerySchema, location="query")
    @platform_tenants_blp.response(200, TenantStatsSchema)
    def get(self, args, garage_id):
        """Per-business statistics over the last ``days`` days."""
        return tenant_stats(_require_tenant(garage_id), days=args.get("days") or 30)


@platform_tenants_blp.route("/tenants/<uuid:garage_id>/feature-flags")
class TenantFeatureFlags(MethodView):
    @jwt_required()
    @platform_admin_required
    @platform_tenants_blp.response(200, FeatureFlagListSchema)
    def get(self, garage_id):
        """This business's effective feature set, and where each value came
        from (its plan, or a deliberate override)."""
        garage = _require_tenant(garage_id)
        return {"plan": garage.plan, "flags": feature_summary(garage)}

    @jwt_required()
    @superadmin_required
    @platform_tenants_blp.arguments(FeatureFlagUpdateSchema)
    @platform_tenants_blp.response(200, FeatureFlagListSchema)
    def put(self, data, garage_id):
        """Override one flag for this business, or clear the override.

        ``enabled: null`` deletes the override and returns the flag to the
        tenant's plan default.
        """
        garage = _require_tenant(garage_id)
        before = {flag["key"]: flag["enabled"] for flag in feature_summary(garage)}

        try:
            set_feature_override(garage, data["key"], data["enabled"])
        except UnknownFeatureError as exc:
            abort(422, message=str(exc))

        db.session.flush()
        after = {flag["key"]: flag["enabled"] for flag in feature_summary(garage)}

        record_audit(
            admin=get_current_platform_admin(),
            action=ACTION_FEATURE_FLAG_UPDATE,
            garage=garage,
            target_type="feature_flag",
            target_id=data["key"],
            summary=(
                f"Set {data['key']} to plan default"
                if data["enabled"] is None
                else f"Set {data['key']} to {'on' if data['enabled'] else 'off'}"
            ),
            details={
                "key": data["key"],
                "override": data["enabled"],
                "from": before.get(data["key"]),
                "to": after.get(data["key"]),
            },
        )
        db.session.commit()

        return {"plan": garage.plan, "flags": feature_summary(garage)}


@platform_tenants_blp.route("/tenants/<uuid:garage_id>/impersonate")
class TenantImpersonate(MethodView):
    @jwt_required()
    @platform_admin_required
    @platform_tenants_blp.arguments(ImpersonationStartSchema)
    @platform_tenants_blp.response(201, ImpersonationGrantSchema)
    def post(self, data, garage_id):
        """Start a support impersonation of this business.

        Returns a **single-use handoff URL**, not a garage token: the console
        opens it, the garage app exchanges it, and the resulting session is
        short-lived, refresh-less, revocable and banner-visible to whoever is
        using it. The owner's password is never read or used.
        """
        garage = _require_tenant(garage_id)
        try:
            return start_impersonation(
                admin=get_current_platform_admin(),
                garage=garage,
                reason=data["reason"],
                employee_id=data.get("employee_id"),
            )
        except ImpersonationError as exc:
            abort(422, message=str(exc))


@platform_tenants_blp.route("/impersonation-sessions")
class ImpersonationSessionList(MethodView):
    @jwt_required()
    @platform_admin_required
    @platform_tenants_blp.arguments(ImpersonationSessionQuerySchema, location="query")
    @platform_tenants_blp.response(200, ImpersonationSessionSchema(many=True))
    def get(self, args):
        """Impersonation sessions, newest first - the live ones and the history."""
        return list_impersonation_sessions(
            garage_id=args.get("garage_id"),
            active_only=bool(args.get("active_only")),
            limit=args.get("limit") or 50,
        )


@platform_tenants_blp.route("/impersonation-sessions/<uuid:session_id>/revoke")
class ImpersonationSessionRevoke(MethodView):
    @jwt_required()
    @platform_admin_required
    @platform_tenants_blp.response(200, ImpersonationSessionSchema)
    def post(self, session_id):
        """End a live impersonation now.

        Takes effect on the impersonated session's very next request, not at
        token expiry. Idempotent.
        """
        session_row = db.session.get(ImpersonationSession, session_id)
        if session_row is None:
            abort(404, message="Impersonation session not found.")
        return revoke_impersonation(admin=get_current_platform_admin(), session_row=session_row)
