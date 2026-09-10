"""The Platform Admin permission boundary.

Three things live here and nowhere else:

* how a platform token is minted (:func:`create_platform_admin_tokens`),
* how a request proves it carries one (:func:`platform_admin_required` /
  :func:`superadmin_required`), and
* how a platform token is revoked (:func:`platform_admin_token_revoked`, called
  from the JWT blocklist loader in ``app/__init__.py``).

The claim ``account_type="platform_admin"`` is necessary but never sufficient:
every guarded request re-loads the :class:`PlatformAdmin` row and re-checks
that the account still exists, is still active, and was not invalidated by a
later password change. A deactivated admin's live token stops working on its
next request.
"""

from __future__ import annotations

import uuid
from functools import wraps

from flask import current_app
from flask_jwt_extended import (
    create_access_token,
    create_refresh_token,
    get_jwt,
    get_jwt_identity,
    verify_jwt_in_request,
)
from flask_smorest import abort

from app.extensions import db
from app.models.platform.admin import ROLE_SUPERADMIN, PlatformAdmin

#: The JWT claim that separates a platform token from a garage-employee token
#: (no ``account_type``) and a customer-portal token (``"customer"``).
ACCOUNT_TYPE_PLATFORM_ADMIN = "platform_admin"


def _claims(admin: PlatformAdmin) -> dict:
    # `role` is a convenience for the admin UI only - every server-side
    # privilege check re-reads the role from the database row, so an admin
    # demoted mid-session loses access immediately rather than at token expiry.
    return {"account_type": ACCOUNT_TYPE_PLATFORM_ADMIN, "role": admin.role}


def create_platform_admin_tokens(admin: PlatformAdmin) -> dict[str, str]:
    """The access + refresh pair for a signed-in platform admin."""
    identity = str(admin.id)
    additional_claims = _claims(admin)
    return {
        "access_token": create_access_token(
            identity=identity,
            additional_claims=additional_claims,
            expires_delta=current_app.config["PLATFORM_ADMIN_ACCESS_TOKEN_EXPIRES"],
        ),
        "refresh_token": create_refresh_token(
            identity=identity,
            additional_claims=additional_claims,
            expires_delta=current_app.config["PLATFORM_ADMIN_REFRESH_TOKEN_EXPIRES"],
        ),
    }


def create_platform_admin_access_token(admin: PlatformAdmin) -> str:
    """A fresh access token for an already-authenticated admin (refresh flow)."""
    token: str = create_access_token(
        identity=str(admin.id),
        additional_claims=_claims(admin),
        expires_delta=current_app.config["PLATFORM_ADMIN_ACCESS_TOKEN_EXPIRES"],
    )
    return token


def resolve_platform_admin(jwt_payload: dict) -> PlatformAdmin | None:
    """The live :class:`PlatformAdmin` a decoded token refers to, or ``None``.

    ``None`` for anything that isn't a currently-valid platform token: a
    garage-employee token, a customer token, a token whose subject no longer
    exists, or a deactivated account.
    """
    if jwt_payload.get("account_type") != ACCOUNT_TYPE_PLATFORM_ADMIN:
        return None

    try:
        admin_id = uuid.UUID(jwt_payload.get("sub"))
    except (TypeError, ValueError):
        return None

    admin = db.session.get(PlatformAdmin, admin_id)
    if admin is None or not admin.is_active:
        return None
    return admin


def platform_admin_token_revoked(jwt_payload: dict) -> bool:
    """Blocklist check for a platform token: unknown, deactivated, or issued
    before the account's last password change."""
    admin = resolve_platform_admin(jwt_payload)
    if admin is None:
        return True

    valid_from = admin.tokens_valid_from
    return valid_from is not None and jwt_payload.get("iat", 0) < valid_from.timestamp()


def get_current_platform_admin() -> PlatformAdmin | None:
    """The platform admin behind the current request, if there is one.

    Deliberately **not** memoised on ``flask.g``. Flask only pushes a fresh
    app context for a request when one isn't already active, so under an
    outer ``app.app_context()`` (the test suite, a CLI command, a nested
    context) several requests share one ``g`` - and a cached identity would
    then leak from the request that resolved it into the next request, which
    may be carrying an entirely different token. Re-resolving is a single
    primary-key lookup that SQLAlchemy serves from its identity map, so the
    cache bought little and risked a great deal.
    """
    if get_jwt_identity() is None:
        return None
    return resolve_platform_admin(get_jwt())


def platform_admin_required(fn):
    """Require a valid, active platform-admin access token.

    Verifies the JWT itself, so it is safe on a view with no other auth
    decorator; stacking it under ``@jwt_required()`` (as the routes do, for
    consistency with the rest of the API) is a no-op re-verification.

    A garage-employee or customer token fails here with 403, not 401 - the
    caller *is* authenticated, just never as a platform admin.
    """

    @wraps(fn)
    def wrapper(*args, **kwargs):
        verify_jwt_in_request()

        if get_current_platform_admin() is None:
            abort(403, message="Platform administrator access required.")

        return fn(*args, **kwargs)

    return wrapper


def superadmin_required(fn):
    """Restrict a ``@platform_admin_required`` view to the SUPERADMIN role.

    SUPPORT admins may read everything and impersonate for support, but never
    change a tenant's configuration, suspend one, or flip a feature flag.
    """

    @wraps(fn)
    def wrapper(*args, **kwargs):
        verify_jwt_in_request()

        admin = get_current_platform_admin()
        if admin is None:
            abort(403, message="Platform administrator access required.")
        if admin.role != ROLE_SUPERADMIN:
            abort(403, message="Superadmin role required for this action.")

        return fn(*args, **kwargs)

    return wrapper
