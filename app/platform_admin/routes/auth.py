"""Platform Admin sign-in.

Its own login endpoint, against its own account table - a garage or customer
credential presented here fails exactly like an unknown address, because the
lookup only ever touches ``platform_admins``.

Both outcomes are audited. A successful sign-in is an event worth being able
to review later; a failed one is the signal that matters most on a console
that can read every tenant, so it is recorded with the address that was tried
(never the password) even though no session results.
"""

from datetime import UTC, datetime

from flask import current_app
from flask.views import MethodView
from flask_jwt_extended import get_jwt, jwt_required
from flask_smorest import Blueprint, abort
from werkzeug.security import check_password_hash

from app.extensions import db, limiter
from app.models.platform.admin import PlatformAdmin
from app.models.platform.audit_log import ACTION_LOGIN, ACTION_LOGIN_FAILED
from app.platform_admin.audit import record_audit
from app.platform_admin.schemas import (
    PlatformAccessTokenSchema,
    PlatformAdminSchema,
    PlatformLoginSchema,
    PlatformTokenSchema,
)
from app.platform_admin.security import (
    create_platform_admin_access_token,
    create_platform_admin_tokens,
    get_current_platform_admin,
    platform_admin_required,
    resolve_platform_admin,
)

platform_auth_blp = Blueprint(
    "platform_admin_auth",
    "platform_admin_auth",
    url_prefix="/api/platform-admin/auth",
    description="Platform Admin authentication (CoMaz staff only)",
)


@platform_auth_blp.route("/login")
class PlatformLogin(MethodView):
    @limiter.limit(lambda: current_app.config["PLATFORM_ADMIN_LOGIN_RATELIMIT"])
    @platform_auth_blp.arguments(PlatformLoginSchema)
    @platform_auth_blp.response(200, PlatformTokenSchema)
    def post(self, data):
        """Sign in as a CoMaz platform administrator.

        One message for "no such admin", "wrong password" and "deactivated",
        so this endpoint can't be used to enumerate internal staff accounts.
        """
        admin = PlatformAdmin.query.filter_by(email=data["email"]).first()

        if (
            admin is None
            or not admin.is_active
            or not check_password_hash(admin.password_hash, data["password"])
        ):
            record_audit(
                action=ACTION_LOGIN_FAILED,
                admin=admin if admin is not None and admin.is_active else None,
                admin_email=data["email"],
                summary="Failed Platform Admin sign-in",
                commit=True,
            )
            abort(401, message="Invalid email or password.")

        admin.last_login_at = datetime.now(UTC)
        tokens = create_platform_admin_tokens(admin)

        record_audit(
            admin=admin,
            action=ACTION_LOGIN,
            summary=f"{admin.display_name} signed in to Platform Admin",
        )
        db.session.commit()

        return tokens


@platform_auth_blp.route("/refresh")
class PlatformRefresh(MethodView):
    @jwt_required(refresh=True)
    @platform_auth_blp.response(200, PlatformAccessTokenSchema)
    def post(self):
        """Exchange a platform refresh token for a new access token.

        Re-resolves the admin from the database, so an account deactivated or
        demoted mid-session cannot refresh its way onward.
        """
        admin = resolve_platform_admin(get_jwt())
        if admin is None:
            abort(401, message="Not authenticated as a platform administrator.")
        return {"access_token": create_platform_admin_access_token(admin)}


@platform_auth_blp.route("/me")
class PlatformMe(MethodView):
    @jwt_required()
    @platform_admin_required
    @platform_auth_blp.response(200, PlatformAdminSchema)
    def get(self):
        """The signed-in administrator, so the console can gate superadmin-only
        UI up front instead of discovering it from a 403."""
        return get_current_platform_admin()
