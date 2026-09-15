from datetime import datetime

from sqlalchemy import JSON, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db


class PaymentWebhookEvent(db.Model):  # type: ignore[name-defined]
    """One received provider webhook event, keyed by the provider's own event
    id - the idempotency ledger for app/payments/webhooks.py.

    A provider (Stripe included) can and does redeliver the same event more
    than once (retries, or the endpoint acknowledging slowly); the webhook
    handler checks for an existing row with this id *before* applying any
    side effect and simply 200s without reprocessing if one exists. No
    PrimaryKeyMixin/TimestampMixin here on purpose - the id IS the provider's
    event id (not a generated UUID), and there is no updated_at: an event is
    write-once.
    """

    __tablename__ = "payment_webhook_events"

    # The provider's own event id, e.g. Stripe's "evt_...". Not our UUID
    # convention - it has to match exactly what the provider sends so a
    # redelivery collides on the same primary key.
    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The provider's event payload, verbatim - Stripe events never carry raw
    # card/PAN data, only references and non-sensitive metadata, so this is
    # safe to persist for audit/debugging.
    payload: Mapped[dict | None] = mapped_column(JSON)
