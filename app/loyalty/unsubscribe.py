"""Signed marketing-email unsubscribe links.

The token names one customer of one business and is signed with SECRET_KEY
under its own salt, so it can't be forged or reused for anything else. It
deliberately never expires: an unsubscribe link in an old email must keep
working (PECR/GDPR expect opting out to stay easy), and all it can ever do
is turn promotional email *off*.
"""

from __future__ import annotations

import uuid
from typing import cast

from flask import current_app
from itsdangerous import BadSignature, URLSafeSerializer

from app.models.customer import Customer

_SALT = "marketing-email-unsubscribe"


def _serializer() -> URLSafeSerializer:
    return URLSafeSerializer(current_app.config["SECRET_KEY"], salt=_SALT)


def make_token(customer: Customer) -> str:
    return str(_serializer().dumps({"c": str(customer.id), "g": str(customer.garage_id)}))


def customer_for_token(token: str | None) -> Customer | None:
    if not token:
        return None
    try:
        data = _serializer().loads(token)
        customer_id = uuid.UUID(data["c"])
        garage_id = uuid.UUID(data["g"])
    except (BadSignature, KeyError, TypeError, ValueError):
        return None
    return cast(
        Customer | None, Customer.query.filter_by(id=customer_id, garage_id=garage_id).first()
    )


def unsubscribe_url(customer: Customer) -> str | None:
    base = (current_app.config.get("APP_BASE_URL") or "").rstrip("/")
    return f"{base}/unsubscribe?token={make_token(customer)}" if base else None
