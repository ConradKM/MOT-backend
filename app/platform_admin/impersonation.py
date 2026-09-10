"""Support impersonation: "log in as this business", safely.

The owner's password is never read, compared, reset or shown. Impersonation
mints a *new* short-lived token for an existing, active employee account, and
records the grant so it can be watched and cut off.

The flow, and why it has three steps instead of one:

1. **Start** (``POST /api/platform-admin/tenants/<id>/impersonate``, audited).
   Creates an :class:`ImpersonationSession` and returns a **single-use handoff
   code** - not a garage token. The admin console cannot itself act as the
   tenant.
2. **Handoff.** The console opens the garage app at ``/impersonate#code=...``.
   A URL *fragment* is never sent to a server, and the code is single-use,
   hashed at rest and valid for about a minute, so the only value that crosses
   the origin boundary is worthless almost immediately.
3. **Exchange** (``POST /api/auth/impersonation/exchange``, unauthenticated -
   the code is the credential). Burns the code and returns the real access
   token: employee-scoped, expiring in
   ``PLATFORM_ADMIN_IMPERSONATION_MINUTES``, carrying ``impersonation_id`` and
   ``impersonated_by`` claims, and with **no refresh token** - the session
   cannot be extended, only re-requested (and re-audited).

Revocation is immediate: :func:`impersonation_token_revoked` is consulted by
the JWT blocklist loader on every request, so setting ``revoked_at`` kills the
token on its very next call rather than at expiry.

An impersonation token is an employee token. It carries no
``account_type="platform_admin"`` claim, so it cannot reach a single
``/api/platform-admin`` route - impersonation can never escalate back into
the platform console.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import cast

from flask import current_app
from flask_jwt_extended import create_access_token

from app.extensions import db
from app.models.employee import Employee
from app.models.garage import Garage
from app.models.platform.audit_log import (
    ACTION_IMPERSONATION_REVOKE,
    ACTION_IMPERSONATION_START,
)
from app.models.platform.impersonation import ImpersonationSession
from app.models.role import Role, employee_roles

from .audit import record_audit

#: JWT claims that mark an employee token as an impersonation.
CLAIM_SESSION_ID = "impersonation_id"
CLAIM_ADMIN_ID = "impersonated_by"
CLAIM_ADMIN_EMAIL = "impersonated_by_email"

MIN_REASON_LENGTH = 8


class ImpersonationError(ValueError):
    """A rejected impersonation request (maps to 422)."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _hash_code(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def _pick_employee(garage: Garage, employee_id: uuid.UUID | None) -> Employee:
    """The account to act as: the one asked for, else this tenant's longest-
    standing active OWNER, else any active employee."""
    if employee_id is not None:
        employee = db.session.get(Employee, employee_id)
        if employee is None or employee.garage_id != garage.id:
            raise ImpersonationError("That employee does not belong to this business.")
        if not employee.is_active:
            raise ImpersonationError("That employee account is deactivated.")
        return employee

    owner = (
        Employee.query.join(employee_roles, employee_roles.c.employee_id == Employee.id)
        .join(Role, Role.id == employee_roles.c.role_id)
        .filter(
            Employee.garage_id == garage.id,
            Employee.is_active.is_(True),
            Role.name == "OWNER",
        )
        .order_by(Employee.created_at)
        .first()
    )
    if owner is not None:
        return cast(Employee, owner)

    fallback = (
        Employee.query.filter(Employee.garage_id == garage.id, Employee.is_active.is_(True))
        .order_by(Employee.created_at)
        .first()
    )
    if fallback is None:
        raise ImpersonationError("This business has no active staff account to impersonate.")
    return cast(Employee, fallback)


def start_impersonation(
    *, admin, garage: Garage, reason: str, employee_id: uuid.UUID | None = None
) -> dict:
    """Open a session and return its one-time handoff code.

    Commits the session row and its audit entry together. The returned code is
    the only time the plaintext exists - only its hash is stored.
    """
    reason = (reason or "").strip()
    if len(reason) < MIN_REASON_LENGTH:
        raise ImpersonationError(
            f"Give a support reason of at least {MIN_REASON_LENGTH} characters - "
            "it is recorded in the audit trail."
        )

    employee = _pick_employee(garage, employee_id)

    now = _utcnow()
    minutes = current_app.config["PLATFORM_ADMIN_IMPERSONATION_MINUTES"]
    handoff_seconds = current_app.config["PLATFORM_ADMIN_IMPERSONATION_HANDOFF_SECONDS"]

    code = secrets.token_urlsafe(32)
    session_row = ImpersonationSession(
        admin_id=getattr(admin, "id", None),
        admin_email=getattr(admin, "email", None),
        garage_id=garage.id,
        employee_id=employee.id,
        reason=reason,
        expires_at=now + timedelta(minutes=minutes),
        handoff_code_hash=_hash_code(code),
        handoff_expires_at=now + timedelta(seconds=handoff_seconds),
    )
    db.session.add(session_row)
    db.session.flush()

    record_audit(
        admin=admin,
        action=ACTION_IMPERSONATION_START,
        garage=garage,
        target_type="impersonation_session",
        target_id=session_row.id,
        summary=f"Started impersonating {employee.email} at {garage.name}",
        details={
            "reason": reason,
            "employee_id": str(employee.id),
            "employee_email": employee.email,
            "expires_at": session_row.expires_at.isoformat(),
            "minutes": minutes,
        },
    )
    db.session.commit()

    app_base_url = (current_app.config.get("APP_BASE_URL") or "").rstrip("/")
    return {
        "session": session_row,
        # Fragment, not query string: never transmitted to the frontend's host.
        "handoff_url": f"{app_base_url}/impersonate#code={code}",
        "handoff_expires_at": session_row.handoff_expires_at,
        "expires_at": session_row.expires_at,
        "employee": employee,
        "garage": garage,
    }


def exchange_handoff_code(code: str) -> dict:
    """Burn a handoff code and mint the impersonation access token.

    Unauthenticated by design - the code *is* the credential. Every failure
    mode (unknown, already used, expired handoff, revoked or expired session,
    deactivated employee) returns the same
    :class:`ImpersonationError`, so the endpoint can't be used to probe which
    codes exist.
    """
    generic = ImpersonationError("This impersonation link is invalid or has expired.")

    if not code:
        raise generic

    session_row = ImpersonationSession.query.filter_by(handoff_code_hash=_hash_code(code)).first()
    if session_row is None:
        raise generic

    now = _utcnow()
    if session_row.handoff_used_at is not None:
        raise generic
    handoff_expires_at = _as_utc(session_row.handoff_expires_at)
    if handoff_expires_at is None or handoff_expires_at <= now:
        raise generic
    if not session_row.is_active(now):
        raise generic

    employee = db.session.get(Employee, session_row.employee_id)
    if employee is None or not employee.is_active:
        raise generic

    session_row.handoff_used_at = now
    db.session.commit()

    # NOT NULL in the schema; assert it so the arithmetic below is total.
    expires_at = _as_utc(session_row.expires_at)
    assert expires_at is not None
    token = create_access_token(
        identity=str(employee.id),
        additional_claims={
            CLAIM_SESSION_ID: str(session_row.id),
            CLAIM_ADMIN_ID: str(session_row.admin_id) if session_row.admin_id else None,
            CLAIM_ADMIN_EMAIL: session_row.admin_email,
        },
        # Hard stop. There is no refresh token, so this is the whole session.
        expires_delta=expires_at - now,
    )

    return {
        "access_token": token,
        "expires_at": expires_at,
        "session": session_row,
        "employee": employee,
        "garage": db.session.get(Garage, session_row.garage_id),
    }


def impersonation_token_revoked(jwt_payload: dict) -> bool:
    """Blocklist check for an employee token that carries impersonation claims.

    Revoked here means: session row gone, revoked by an admin, or past its
    expiry. Called on every request, so revocation takes effect immediately.
    """
    raw_id = jwt_payload.get(CLAIM_SESSION_ID)
    try:
        session_id = uuid.UUID(raw_id)
    except (TypeError, ValueError):
        return True

    session_row = db.session.get(ImpersonationSession, session_id)
    if session_row is None:
        return True
    return not session_row.is_active()


def revoke_impersonation(*, admin, session_row: ImpersonationSession) -> ImpersonationSession:
    """End a live session now. Idempotent - re-revoking is a no-op, not an
    error, so two admins clicking at once don't produce a failure."""
    if session_row.revoked_at is None:
        session_row.revoked_at = _utcnow()
        session_row.revoked_by_admin_id = getattr(admin, "id", None)

        record_audit(
            admin=admin,
            action=ACTION_IMPERSONATION_REVOKE,
            garage=session_row.garage,
            target_type="impersonation_session",
            target_id=session_row.id,
            summary=f"Revoked impersonation of {session_row.garage.name}",
            details={"started_by": session_row.admin_email},
        )
        db.session.commit()

    return session_row


def list_impersonation_sessions(
    *, garage_id=None, active_only: bool = False, limit: int = 50
) -> list[ImpersonationSession]:
    query = ImpersonationSession.query
    if garage_id is not None:
        query = query.filter(ImpersonationSession.garage_id == garage_id)
    if active_only:
        query = query.filter(
            ImpersonationSession.revoked_at.is_(None),
            ImpersonationSession.expires_at > _utcnow(),
        )
    rows = query.order_by(ImpersonationSession.created_at.desc()).limit(limit).all()
    return cast(list[ImpersonationSession], rows)
