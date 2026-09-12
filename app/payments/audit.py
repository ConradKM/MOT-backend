from datetime import UTC, datetime

from app.extensions import db
from app.models.payments.audit_log import PaymentAuditLog


def record_payment_event(
    *,
    garage_id,
    action: str,
    summary: str,
    booking_request_id=None,
    payment_id=None,
    details: dict | None = None,
) -> None:
    """Append one row to the payment audit log. Does not commit - callers
    add this alongside their own state change and commit once, so the audit
    row and the state it describes land in the same transaction (see
    app/payments/service.py)."""
    db.session.add(
        PaymentAuditLog(
            created_at=datetime.now(UTC),
            garage_id=garage_id,
            booking_request_id=booking_request_id,
            payment_id=payment_id,
            action=action,
            summary=summary,
            details=details,
        )
    )
