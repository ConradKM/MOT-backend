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
from app.models.platform.audit_log import ACTION_FEATURE_FLAG_UPDATE
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
from app.platform_admin.schemas import (
    FeatureFlagListSchema,
    FeatureFlagUpdateSchema,
    ImpersonationGrantSchema,
    ImpersonationSessionQuerySchema,
    ImpersonationSessionSchema,
    ImpersonationStartSchema,
    OnboardingProgressSchema,
    PeriodQuerySchema,
    TenantDetailSchema,
    TenantListQuerySchema,
    TenantListSchema,
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
                sort=args.get("sort") or "created_at",
                descending=(args.get("order") or "desc") == "desc",
                page=args.get("page") or 1,
                per_page=args.get("per_page") or 25,
            )
        except TenantError as exc:
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


@platform_tenants_blp.route("/tenants/<uuid:garage_id>/onboarding")
class TenantOnboarding(MethodView):
    @jwt_required()
    @platform_admin_required
    @platform_tenants_blp.response(200, OnboardingProgressSchema)
    def get(self, garage_id):
        """How far this business has got with setting itself up - derived from
        its own data, never from a stored progress flag."""
        return onboarding_progress(_require_tenant(garage_id))


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
