"""Shared garage-account creation.

Used by garage onboarding (POST /api/auth/register - the first OWNER) and by
an owner adding staff (POST /api/employees/). There is no public/anonymous
path to this beyond onboarding a brand-new garage.
"""

from flask_smorest import abort
from werkzeug.security import generate_password_hash

from app.extensions import db
from app.models.employee import Employee

# The application password policy - one place, reused by onboarding, staff
# creation and password reset.
PASSWORD_MIN_LENGTH = 8


def validate_password(password: str) -> None:
    if not password or len(password) < PASSWORD_MIN_LENGTH:
        abort(
            422,
            message=f"Password must be at least {PASSWORD_MIN_LENGTH} characters.",
        )


def create_employee_account(
    *,
    garage_id,
    email: str,
    password: str,
    first_name: str | None = None,
    last_name: str | None = None,
    roles=None,
) -> Employee:
    """Create + flush an Employee login account. Aborts 409 on a duplicate
    email (emails are globally unique). Never stores the plaintext password."""
    validate_password(password)

    if Employee.query.filter_by(email=email).first() is not None:
        abort(409, message="An account with this email already exists.")

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
