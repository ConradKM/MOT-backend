import uuid

from flask_jwt_extended import get_jwt, get_jwt_identity

from app.extensions import db
from app.models.customer import Customer

# Marks a token as belonging to a customer (see app/customer_auth/routes.py)
# rather than a garage employee. Both identities are a bare UUID string, so
# this claim is what keeps the two apart - customer tokens are rejected by
# app/auth/utils.get_current_employee, and staff tokens by get_current_customer.
CUSTOMER_ACCOUNT_TYPE = "customer"


def get_current_customer() -> Customer | None:
    """The Customer for the request's JWT, or None if it isn't a customer token."""
    if get_jwt().get("account_type") != CUSTOMER_ACCOUNT_TYPE:
        return None

    identity = get_jwt_identity()
    if identity is None:
        return None

    return db.session.get(Customer, uuid.UUID(identity))
