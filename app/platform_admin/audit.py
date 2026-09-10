"""Writing the platform audit trail.

Every sensitive Platform Admin action funnels through :func:`record_audit`,
which snapshots *who* (admin id + email), *what* (a dotted action constant, a
human summary and structured details), *which tenant* (id + name) and *from
where* (IP, user agent).

What never reaches this table: passwords, password hashes, tokens, and
customer personal data. ``details`` is for platform-side facts - which fields
changed, the reason an admin typed, a suspension note - and
:func:`_scrub` drops anything whose key looks like a secret, as a backstop
against a caller passing one by accident.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from flask import has_request_context, request
from sqlalchemy import func, or_, select

from app.extensions import db
from app.models.platform.audit_log import PlatformAuditLog

logger = logging.getLogger(__name__)

# Substrings that mark a key as never-to-be-stored. Matched case-insensitively
# against every key in `details`.
_SECRET_KEY_HINTS = ("password", "token", "secret", "api_key", "authorization", "hash")

_MAX_USER_AGENT = 300


def _scrub(details: dict[str, Any] | None) -> dict[str, Any] | None:
    if not details:
        return None
    return {
        key: value
        for key, value in details.items()
        if not any(hint in key.lower() for hint in _SECRET_KEY_HINTS)
    }


def _client_ip() -> str | None:
    if not has_request_context():
        return None
    # X-Forwarded-For's first hop is the client when the app sits behind the
    # platform's own proxy (Render). Falls back to the socket peer.
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()[:45] or None
    return request.remote_addr or None


def _user_agent() -> str | None:
    if not has_request_context():
        return None
    user_agent = request.headers.get("User-Agent")
    return user_agent[:_MAX_USER_AGENT] if user_agent else None


def record_audit(
    *,
    admin=None,
    action: str,
    garage=None,
    target_type: str | None = None,
    target_id: str | uuid.UUID | None = None,
    summary: str | None = None,
    details: dict[str, Any] | None = None,
    admin_email: str | None = None,
    commit: bool = False,
) -> PlatformAuditLog:
    """Append one audit row.

    ``commit=False`` (the default) leaves the row in the caller's transaction,
    so an audited action and its audit entry commit together - an action can
    never succeed without its audit row, and a rolled-back action leaves no
    misleading trail. Pass ``commit=True`` only where there is no surrounding
    unit of work (a failed login, which has nothing else to write).

    ``admin_email`` is a fallback for the one case with no admin row to read
    it from - a login attempt against an unknown address.
    """
    entry = PlatformAuditLog(
        admin_id=getattr(admin, "id", None),
        admin_email=getattr(admin, "email", None) or admin_email,
        action=action,
        garage_id=getattr(garage, "id", None),
        garage_name=getattr(garage, "name", None),
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        summary=summary,
        details=_scrub(details),
        ip_address=_client_ip(),
        user_agent=_user_agent(),
    )
    db.session.add(entry)

    if commit:
        db.session.commit()
    else:
        db.session.flush()

    logger.info(
        "[platform-admin] %s by %s%s",
        action,
        entry.admin_email or "unknown",
        f" on tenant {entry.garage_name}" if entry.garage_name else "",
    )
    return entry


def list_audit_logs(
    *,
    admin_id=None,
    garage_id=None,
    action: str | None = None,
    search: str | None = None,
    page: int = 1,
    per_page: int = 50,
) -> dict:
    """One page of the audit trail, newest first.

    Read-only: nothing in the API updates or deletes an entry, so what an
    admin sees here is what was written at the time of the action.
    """
    query = select(PlatformAuditLog)
    if admin_id is not None:
        query = query.where(PlatformAuditLog.admin_id == admin_id)
    if garage_id is not None:
        query = query.where(PlatformAuditLog.garage_id == garage_id)
    if action:
        query = query.where(PlatformAuditLog.action == action)
    if search:
        like = f"%{search.strip()}%"
        query = query.where(
            or_(
                PlatformAuditLog.summary.ilike(like),
                PlatformAuditLog.admin_email.ilike(like),
                PlatformAuditLog.garage_name.ilike(like),
                PlatformAuditLog.action.ilike(like),
            )
        )

    per_page = max(1, min(per_page or 50, 200))
    page = max(1, page or 1)
    total = db.session.scalar(select(func.count()).select_from(query.subquery())) or 0
    items = (
        db.session.execute(
            query.order_by(PlatformAuditLog.created_at.desc(), PlatformAuditLog.id)
            .limit(per_page)
            .offset((page - 1) * per_page)
        )
        .scalars()
        .all()
    )

    return {
        "items": items,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page,
    }
