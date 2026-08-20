from functools import wraps

from flask import abort

from .utils import get_current_employee


def owner_required(fn):
    """Restrict an already-@jwt_required() view to the OWNER role."""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        employee = get_current_employee()

        if employee is None or not employee.has_role("OWNER"):
            abort(403, description="Owner role required.")

        return fn(*args, **kwargs)

    return wrapper
