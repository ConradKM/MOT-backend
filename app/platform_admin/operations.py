"""Operational health: email delivery, communication failures, job health.

The support-desk half of Platform Admin. Everything here reads tables the
product already writes - there is no separate ops store to keep in sync:

* **Email delivery** is ``CommunicationLog`` rows with ``channel="EMAIL"``.
  ``app/email/service.py`` already logs every attempt, sent or failed, so this
  is the existing record surfaced rather than a new one.
* **Resending** a failure re-sends through ``app.email.send_email`` - the same
  provider abstraction the product uses - and writes a **new** log row linked
  back to the failure by ``retry_of_id``. The original row is never mutated: a
  failure that happened stays in the history, with its retry attached.
* **Job health** is inferred from evidence rather than from a scheduler we
  don't control. Nothing in this deployment records "the reminder job ran at
  10:00"; what it does record is the reminders that job produced, the requests
  the expiry sweep should have retired, and the sends that failed. Each check
  below states what it actually measured, so an amber light is a fact about
  the data, never a guess about a process.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

from flask import current_app
from sqlalchemy import func, or_, select

from app.communications.events import (
    APPOINTMENT_COMPLETED,
    APPOINTMENT_CREATED,
    APPOINTMENT_RESCHEDULED,
    BOOKING_REQUEST_CREATED,
    BOOKING_REQUEST_REJECTED,
)
from app.email import send_email
from app.extensions import db
from app.models.booking_request import BookingRequest
from app.models.communications.communication_log import (
    CHANNEL_EMAIL,
    DIRECTION_OUTBOUND,
    CommunicationLog,
)
from app.models.garage import Garage
from app.models.platform.audit_log import ACTION_EMAIL_RESEND
from app.models.reminder import STATUS_PENDING, TRIGGER_AUTOMATIC, Reminder

from .audit import record_audit
from .stats import FAILED_STATUSES

logger = logging.getLogger(__name__)

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

STATUS_SENT = "SENT"
STATUS_FAILED = "FAILED"

#: Subject fallback for rows written before CommunicationLog.subject existed.
#: Mirrors the literals in app/email/service.py; a trigger with no entry falls
#: back to the tenant's own name, never to an empty subject line.
_SUBJECT_BY_TRIGGER = {
    BOOKING_REQUEST_CREATED: "We've received your booking request",
    BOOKING_REQUEST_REJECTED: "About your booking request",
    APPOINTMENT_CREATED: "Your appointment is confirmed",
    APPOINTMENT_RESCHEDULED: "Your appointment has been updated",
    APPOINTMENT_COMPLETED: "Your appointment summary",
}


class OperationsError(ValueError):
    """A rejected operations action (maps to 422)."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _paginate(query, page: int, per_page: int) -> dict:
    per_page = max(1, min(per_page or DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE))
    page = max(1, page or 1)
    total = db.session.scalar(select(func.count()).select_from(query.subquery())) or 0
    items = list(
        db.session.execute(query.limit(per_page).offset((page - 1) * per_page)).scalars().all()
    )
    return {
        "items": items,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page,
    }


# --------------------------------------------------------------------------
# Email delivery log
# --------------------------------------------------------------------------


def list_emails(
    *,
    status: str | None = None,
    garage_id: uuid.UUID | None = None,
    search: str | None = None,
    days: int | None = None,
    page: int = 1,
    per_page: int = DEFAULT_PAGE_SIZE,
) -> dict:
    """The email delivery log, newest first.

    ``status`` accepts "failed" or "sent" as convenience buckets over the
    free-text provider statuses, or an exact status value.
    """
    query = select(CommunicationLog).where(CommunicationLog.channel == CHANNEL_EMAIL)

    if garage_id is not None:
        query = query.where(CommunicationLog.garage_id == garage_id)
    if days:
        query = query.where(CommunicationLog.created_at >= _utcnow() - timedelta(days=int(days)))
    if status == "failed":
        query = query.where(CommunicationLog.status.in_(FAILED_STATUSES))
    elif status == "sent":
        query = query.where(CommunicationLog.status.notin_(FAILED_STATUSES))
    elif status:
        query = query.where(CommunicationLog.status == status)
    if search:
        like = f"%{search.strip()}%"
        query = query.where(
            or_(
                CommunicationLog.to_address.ilike(like),
                CommunicationLog.subject.ilike(like),
                CommunicationLog.trigger_event.ilike(like),
            )
        )

    return _paginate(query.order_by(CommunicationLog.created_at.desc()), page, per_page)


def email_failure_summary(*, days: int = 7) -> dict:
    """Headline counts for the operations dashboard."""
    since = _utcnow() - timedelta(days=days)
    base = [CommunicationLog.channel == CHANNEL_EMAIL, CommunicationLog.created_at >= since]

    total = db.session.scalar(select(func.count()).select_from(CommunicationLog).where(*base)) or 0
    failed = (
        db.session.scalar(
            select(func.count())
            .select_from(CommunicationLog)
            .where(*base, CommunicationLog.status.in_(FAILED_STATUSES))
        )
        or 0
    )
    return {
        "period_days": days,
        "total": total,
        "failed": failed,
        "sent": total - failed,
        "failure_rate": round(100 * failed / total, 1) if total else None,
    }


def _subject_for(log: CommunicationLog, garage: Garage) -> str:
    return (
        log.subject
        or _SUBJECT_BY_TRIGGER.get(log.trigger_event or "", "")
        or (f"A message from {garage.name}")
    )


def resend_email(*, admin, log: CommunicationLog) -> CommunicationLog:
    """Re-send one failed email and record the attempt as its own log row.

    Only a failed ``EMAIL`` row can be resent, and only to the address it was
    originally addressed to - the recipient is read from the stored row, never
    supplied by the caller, so this can't be turned into an open relay. The
    original row is left exactly as it was.
    """
    if log.channel != CHANNEL_EMAIL:
        raise OperationsError("Only email messages can be resent from here.")
    if log.status not in FAILED_STATUSES:
        raise OperationsError("Only a failed email can be resent.")
    if not log.to_address:
        raise OperationsError("This message has no recipient address to resend to.")
    if not log.body:
        raise OperationsError("This message has no stored body to resend.")

    garage = db.session.get(Garage, log.garage_id)
    if garage is None:
        raise OperationsError("The tenant this message belongs to no longer exists.")

    subject = _subject_for(log, garage)

    retry = CommunicationLog(
        garage_id=log.garage_id,
        customer_id=log.customer_id,
        appointment_id=log.appointment_id,
        booking_request_id=log.booking_request_id,
        channel=CHANNEL_EMAIL,
        direction=DIRECTION_OUTBOUND,
        external_provider=current_app.config.get("EMAIL_PROVIDER", "console"),
        from_address=log.from_address,
        to_address=log.to_address,
        subject=subject,
        trigger_event=log.trigger_event,
        body=log.body,
        status=STATUS_SENT,
        retry_of_id=log.id,
    )

    try:
        send_email(
            to=log.to_address,
            subject=subject,
            body=log.body,
            from_name=garage.name,
            reply_to=log.from_address,
        )
    except Exception as exc:
        logger.exception("[platform-admin] email resend failed for log %s", log.id)
        retry.status = STATUS_FAILED
        retry.error_message = str(exc)

    db.session.add(retry)
    db.session.flush()

    record_audit(
        admin=admin,
        action=ACTION_EMAIL_RESEND,
        garage=garage,
        target_type="communication_log",
        target_id=log.id,
        summary=f"Resent a failed email to {log.to_address} ({retry.status})",
        details={
            "original_id": str(log.id),
            "retry_id": str(retry.id),
            "trigger_event": log.trigger_event,
            "outcome": retry.status,
        },
    )
    db.session.commit()
    return retry


# --------------------------------------------------------------------------
# Communication failures (every channel)
# --------------------------------------------------------------------------


def communication_failures(*, days: int = 7, limit: int = 50) -> dict:
    """Recent failed communications across every channel and tenant, plus the
    per-tenant and per-channel breakdown behind them."""
    since = _utcnow() - timedelta(days=days)
    failed = [
        CommunicationLog.created_at >= since,
        CommunicationLog.status.in_(FAILED_STATUSES),
    ]

    by_channel: dict[str, int] = {
        row[0]: row[1]
        for row in db.session.execute(
            select(CommunicationLog.channel, func.count())
            .where(*failed)
            .group_by(CommunicationLog.channel)
        ).all()
    }

    by_tenant = db.session.execute(
        select(Garage.id, Garage.name, func.count())
        .join(CommunicationLog, CommunicationLog.garage_id == Garage.id)
        .where(*failed)
        .group_by(Garage.id, Garage.name)
        .order_by(func.count().desc())
        .limit(20)
    ).all()

    # Grouped by the output label, not by a second coalesce() expression:
    # repeating the call would emit a different bind parameter, and Postgres
    # then refuses to match it against the one in the SELECT list.
    by_error = db.session.execute(
        select(
            func.coalesce(CommunicationLog.error_code, "UNKNOWN").label("error_code"),
            func.count(),
        )
        .where(*failed)
        .group_by("error_code")
        .order_by(func.count().desc())
        .limit(10)
    ).all()

    recent = (
        db.session.execute(
            select(CommunicationLog)
            .where(*failed)
            .order_by(CommunicationLog.created_at.desc())
            .limit(max(1, min(limit, MAX_PAGE_SIZE)))
        )
        .scalars()
        .all()
    )

    return {
        "period_days": days,
        "total": sum(by_channel.values()),
        "by_channel": by_channel,
        "by_tenant": [
            {"garage_id": garage_id, "garage_name": name, "failures": count}
            for garage_id, name, count in by_tenant
        ],
        "by_error_code": [{"error_code": code, "failures": count} for code, count in by_error],
        "recent": recent,
    }


# --------------------------------------------------------------------------
# Background job health
# --------------------------------------------------------------------------

OK = "ok"
WARNING = "warning"
CRITICAL = "critical"
UNKNOWN = "unknown"

#: An automatic reminder run is expected at least this often (the Celery beat
#: schedule for `send_due_reminders` is hourly in the documented setup). We
#: allow a generous multiple of that before calling it stale, because a
#: perfectly healthy deployment simply has nothing to send some days.
REMINDER_STALE_HOURS = 48
STUCK_REMINDER_HOURS = 6


def _check(key: str, label: str, status: str, detail: str, **extra) -> dict:
    return {"key": key, "label": label, "status": status, "detail": detail, **extra}


def job_health(*, now: datetime | None = None) -> dict:
    """Health checks for the scheduled work this deployment depends on.

    Each check reports what was measured, not an opinion about a process we
    can't see. ``unknown`` is used deliberately where there is no evidence
    either way - it is more useful than a green light nothing verified.
    """
    now = now or _utcnow()
    checks: list[dict] = []

    # --- MOT reminder job (app/tasks/celery_app.py::send_due_reminders) -----
    broker = current_app.config.get("CELERY_BROKER_URL") or ""
    checks.append(
        _check(
            "celery_broker",
            "Celery broker configured",
            OK if broker else WARNING,
            (
                "A broker URL is configured for the reminder worker."
                if broker
                else "No CELERY_BROKER_URL is set - scheduled reminder sends will not run "
                "unless something else invokes send_due_automatic_reminders()."
            ),
        )
    )

    last_automatic = db.session.scalar(
        select(func.max(Reminder.created_at)).where(Reminder.trigger == TRIGGER_AUTOMATIC)
    )
    total_automatic = (
        db.session.scalar(
            select(func.count()).select_from(Reminder).where(Reminder.trigger == TRIGGER_AUTOMATIC)
        )
        or 0
    )
    if total_automatic == 0:
        reminder_status = UNKNOWN
        reminder_detail = (
            "No automatic reminder has ever been recorded, so there is nothing to "
            "measure yet. Expected on a new deployment."
        )
    else:
        # total_automatic > 0 guarantees MAX(created_at) found a row.
        assert last_automatic is not None
        age_hours = (now - _as_utc(last_automatic)).total_seconds() / 3600
        stale = age_hours > REMINDER_STALE_HOURS
        reminder_status = WARNING if stale else OK
        reminder_detail = f"Last automatic reminder was recorded {age_hours:.0f}h ago" + (
            f" - more than the {REMINDER_STALE_HOURS}h this check expects. That is normal "
            "if no vehicle came due; investigate if it persists."
            if stale
            else "."
        )
    checks.append(
        _check(
            "mot_reminder_job",
            "MOT reminder sends",
            reminder_status,
            reminder_detail,
            last_run_at=last_automatic,
            total=total_automatic,
        )
    )

    stuck = (
        db.session.scalar(
            select(func.count())
            .select_from(Reminder)
            .where(
                Reminder.status == STATUS_PENDING,
                Reminder.created_at < now - timedelta(hours=STUCK_REMINDER_HOURS),
            )
        )
        or 0
    )
    checks.append(
        _check(
            "stuck_reminders",
            "Reminders stuck pending",
            OK if stuck == 0 else CRITICAL,
            (
                "No reminder has been sitting in PENDING."
                if stuck == 0
                else f"{stuck} reminder(s) have been PENDING for over {STUCK_REMINDER_HOURS}h."
            ),
            count=stuck,
        )
    )

    # --- Booking-request expiry sweep --------------------------------------
    # expire_stale_booking_requests() runs on every staff read of the queue,
    # so a backlog here means a tenant whose staff have not opened the app -
    # exactly the signal support wants.
    stale_requests = (
        db.session.scalar(
            select(func.count())
            .select_from(BookingRequest)
            .where(
                BookingRequest.status == "PENDING",
                BookingRequest.preferred_date < now.date(),
            )
        )
        or 0
    )
    checks.append(
        _check(
            "stale_booking_requests",
            "Expired booking requests not swept",
            OK if stale_requests == 0 else WARNING,
            (
                "No pending request is past its preferred date."
                if stale_requests == 0
                else f"{stale_requests} pending request(s) are past their preferred date. "
                "They are swept when staff open the queue, so this usually means a "
                "tenant is not looking at its requests."
            ),
            count=stale_requests,
        )
    )

    # --- Outbound delivery --------------------------------------------------
    failures_24h = (
        db.session.scalar(
            select(func.count())
            .select_from(CommunicationLog)
            .where(
                CommunicationLog.created_at >= now - timedelta(hours=24),
                CommunicationLog.status.in_(FAILED_STATUSES),
            )
        )
        or 0
    )
    checks.append(
        _check(
            "communication_failures_24h",
            "Communication failures (24h)",
            OK if failures_24h == 0 else (WARNING if failures_24h < 10 else CRITICAL),
            (
                "No failed sends in the last 24 hours."
                if failures_24h == 0
                else f"{failures_24h} communication(s) failed in the last 24 hours."
            ),
            count=failures_24h,
        )
    )

    severity = {OK: 0, UNKNOWN: 1, WARNING: 2, CRITICAL: 3}
    worst = max(checks, key=lambda check: severity[check["status"]])["status"]

    return {"status": worst, "checked_at": now, "checks": checks}


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
