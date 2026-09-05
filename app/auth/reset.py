"""Password-reset token helpers (garage users).

Tokens are cryptographically random, stored only as a SHA-256 hash, single
use, and expire (PASSWORD_RESET_TOKEN_MINUTES, ~30). A successful reset also
bumps Employee.tokens_valid_from so live sessions are ended.
"""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from flask import current_app
from werkzeug.security import generate_password_hash

from app.branding import PLATFORM_NAME
from app.email import send_email
from app.extensions import db
from app.models.employee import Employee
from app.models.password_reset_token import PasswordResetToken


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def issue_reset_token(employee: Employee) -> str:
    """New reset token for ``employee``; voids any it already has. Returns the
    raw token - it only ever goes into the emailed link."""
    now = datetime.now(UTC)
    PasswordResetToken.query.filter_by(
        employee_id=employee.id, used_at=None
    ).update({"used_at": now})

    raw = secrets.token_urlsafe(32)
    minutes = current_app.config.get("PASSWORD_RESET_TOKEN_MINUTES", 30)
    db.session.add(
        PasswordResetToken(
            employee_id=employee.id,
            token_hash=_hash_token(raw),
            expires_at=now + timedelta(minutes=minutes),
        )
    )
    return raw


def send_reset_link(employee: Employee, raw_token: str) -> None:
    base = current_app.config.get(
        "APP_BASE_URL", "http://localhost:5173"
    ).rstrip("/")
    minutes = current_app.config.get("PASSWORD_RESET_TOKEN_MINUTES", 30)
    url = f"{base}/reset-password?token={raw_token}"
    send_email(
        to=employee.email,
        subject=f"Reset your {PLATFORM_NAME} password",
        body=(
            "We received a request to reset the password for this account.\n\n"
            f"Reset it here (link expires in {minutes} minutes):\n{url}\n\n"
            "If you didn't ask for this, you can ignore this email."
        ),
    )


def find_valid_token(raw_token: str) -> PasswordResetToken | None:
    if not raw_token:
        return None
    row = PasswordResetToken.query.filter_by(
        token_hash=_hash_token(raw_token)
    ).first()
    if row is None or row.used_at is not None:
        return None
    if row.expires_at <= datetime.now(UTC):
        return None
    return row


def consume_token_and_set_password(
    row: PasswordResetToken, new_password: str
) -> None:
    now = datetime.now(UTC)
    employee = row.employee
    employee.password_hash = generate_password_hash(new_password)
    employee.tokens_valid_from = now
    row.used_at = now
    PasswordResetToken.query.filter(
        PasswordResetToken.employee_id == employee.id,
        PasswordResetToken.id != row.id,
        PasswordResetToken.used_at.is_(None),
    ).update({"used_at": now})
