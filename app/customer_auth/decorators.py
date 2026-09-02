from functools import wraps

from flask_smorest import abort

from .utils import get_current_customer


def customer_required(fn):
    """Restrict an already-@jwt_required() view to a customer token.

    Mirrors app/auth/decorators.owner_required: assumes JWT verification has
    already run, and only checks the identity resolves to a customer.
    """

    @wraps(fn)
    def wrapper(*args, **kwargs):
        if get_current_customer() is None:
            abort(401, message="Customer authentication required.")

        return fn(*args, **kwargs)

    return wrapper
