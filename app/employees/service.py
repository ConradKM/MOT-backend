"""Shared garage-account creation and the application password policy.

One place for "what is a valid password / email" and "how do we build an
Employee login", reused by:

* garage onboarding (``app/garages/onboarding.py`` - the first OWNER), and
* an owner adding staff (``POST /api/employees/``).

There is no public/anonymous path to account creation beyond onboarding a
brand-new garage.

The ``*_error`` helpers are pure (return a message or ``None``) so non-HTTP
callers like the onboarding CLI can raise their own exceptions; the
``validate_*`` / ``create_*`` wrappers add flask-smorest ``abort()`` for the
request path.
"""

import re

from flask_smorest import abort
from werkzeug.security import generate_password_hash

from app.extensions import db
from app.models.employee import Employee

# The application password policy - one number, one place.
PASSWORD_MIN_LENGTH = 8

# Deliberately loose: schema-level `fields.Email` does the strict check on the
# HTTP path; this is the backstop for CLI / service callers.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def password_policy_error(password: str) -> str | None:
    """A message if ``password`` violates the policy, else ``None``."""
    if not password or len(password) < PASSWORD_MIN_LENGTH:
        return f"Password must be at least {PASSWORD_MIN_LENGTH} characters."
    return None


def email_format_error(email: str) -> str | None:
    """A message if ``email`` isn't a plausible address, else ``None``."""
    if not email or not _EMAIL_RE.match(email):
        return "Enter a valid email address."
    return None


def email_in_use(email: str) -> bool:
    """True if a login account already exists for ``email`` (globally unique)."""
    return Employee.query.filter_by(email=email).first() is not None


def validate_password(password: str) -> None:
    """Request-path guard: 422 on a policy violation."""
    message = password_policy_error(password)
    if message:
        abort(422, message=message)


def build_employee_account(
    *,
    garage_id,
    email: str,
    password: str,
    first_name: str | None = None,
    last_name: str | None = None,
    roles=None,
) -> Employee:
    """Construct + add + flush an Employee. No validation and no ``abort`` -
    callers are expected to have validated ``email`` / ``password`` already.
    Never stores the plaintext password."""
    employee = Employee(
        garage_id=garage_id,
        email=email,
        first_name=first_name,
        last_name=last_name,
        password_hash=generate_password_hash(password),
        roles=roles or [],
    )
    db.session.add(employee)
    db.session.flush()
    return employee


def create_employee_account(
    *,
    garage_id,
    email: str,
    password: str,
    first_name: str | None = None,
    last_name: str | None = None,
    roles=None,
) -> Employee:
    """Request-path account creation: 422 on a weak password, 409 on a
    duplicate email, otherwise build + flush the Employee."""
    validate_password(password)

    if email_in_use(email):
        abort(409, message="An account with this email already exists.")

    return build_employee_account(
        garage_id=garage_id,
        email=email,
        password=password,
        first_name=first_name,
        last_name=last_name,
        roles=roles,
    )
