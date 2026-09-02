import uuid

from flask_jwt_extended import get_jwt, get_jwt_identity

from app.extensions import db
from app.models.employee import Employee


def get_current_employee() -> Employee | None:
    employee_id = get_jwt_identity()

    if employee_id is None:
        return None

    # A customer-scoped token (see app/customer_auth) uses a bare UUID identity
    # just like an employee token - the account_type claim is what tells them
    # apart. Never let a customer token resolve to an employee.
    if get_jwt().get("account_type") == "customer":
        return None

    return db.session.get(Employee, uuid.UUID(employee_id))
