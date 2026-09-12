import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db


class PaymentAuditLog(db.Model):  # type: ignore[name-defined]
    """Append-only record of significant payment actions - deposit config
    changed, payment created/succeeded/failed, refund initiated/succeeded/
    failed. Scoped to a garage (unlike PlatformAuditLog, which is Platform
    Admin-only - see app/platform_admin/audit.py), written via
    app/payments/audit.py::record_payment_event.

    Deliberately narrow: only state-changing events are logged (see callers),
    not every inbound webhook - that's PaymentWebhookEvent's job.
    """

    __tablename__ = "payment_audit_log"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    garage_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("garages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    booking_request_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("booking_requests.id", ondelete="SET NULL")
    )
    payment_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("booking_payments.id", ondelete="SET NULL")
    )
    # Dotted free-text action, e.g. "payment.deposit.created",
    # "payment.deposit.succeeded", "payment.refund.initiated" - no enum/
    # migration needed to add a new one, matching PlatformAuditLog's
    # convention.
    action: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    summary: Mapped[str] = mapped_column(String(500), nullable=False)
    details: Mapped[dict | None] = mapped_column(JSON)
