from functools import wraps

from flask import abort

from .utils import get_current_user


def owner_required(fn):
    """Restrict an already-@jwt_required() view to the OWNER role."""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = get_current_user()

        if user is None or not user.has_role("OWNER"):
            abort(403, description="Owner role required.")

        return fn(*args, **kwargs)

    return wrapper
