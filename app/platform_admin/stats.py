"""Statistics for Platform Admin - per tenant, and across the platform.

Read-only aggregation over the tables the product already writes. There is no
statistics table, no counter to keep in sync and no nightly rollup: every
number here is computed from the same rows the garage app itself reads, so a
figure shown to the platform team can never disagree with what the tenant
sees.

Two rules everything in here follows:

* **Aggregate in SQL.** Counts are ``GROUP BY`` queries, not Python loops over
  fetched rows - the platform view spans every tenant, so a per-row round trip
  would get slower with every business onboarded.
* **Rates are honest about small numbers.** A ratio over an empty denominator
  is ``None``, never ``0``. "No booking requests yet" and "every request was
  rejected" must not render as the same 0%.

Revenue is deliberately absent rather than faked. The hooks a future
subscription metric needs - ``Garage.plan``, ``Garage.trial_ends_at`` and the
audited ``tenant.plan_change`` events - are in place, and
:func:`platform_overview` already reports the tenant count per plan; turning
that into MRR needs prices, which live outside this system today.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import Date, case, cast, func, select

from app.extensions import db
from app.models.appointments.appointment import Appointment
from app.models.appointments.appointment_checklist import AppointmentChecklist
from app.models.appointments.appointment_checklist_item import AppointmentChecklistItem
from app.models.booking_request import BookingRequest
from app.models.communications.communication_log import CommunicationLog
from app.models.customer import Customer
from app.models.garage import (
    GARAGE_STATUS_ACTIVE,
    GARAGE_STATUS_SUSPENDED,
    GARAGE_STATUS_TRIAL,
    Garage,
)
from app.models.reminder import Reminder
from app.models.vehicle import Vehicle

DEFAULT_PERIOD_DAYS = 30
MAX_PERIOD_DAYS = 730

#: A communication row in one of these states did not reach the recipient.
#: Free text at the DB level (see CommunicationLog), so this is a *set of
#: known-bad values* rather than an enum - anything else counts as delivered
#: or in flight, and SKIPPED_NOT_CONFIGURED is called out separately because
#: it means "this tenant has not turned the channel on", not "delivery broke".
FAILED_STATUSES = ("FAILED", "failed", "undelivered", "UNDELIVERED", "ERROR")
SKIPPED_STATUSES = ("SKIPPED_NOT_CONFIGURED",)
DELIVERED_STATUSES = ("SENT", "sent", "delivered", "DELIVERED", "read", "received")

#: How long after a reminder an appointment still counts as that reminder
#: converting.
REMINDER_CONVERSION_WINDOW_DAYS = 45


def _utcnow() -> datetime:
    return datetime.now(UTC)


def clamp_days(days: int | None) -> int:
    if not days:
        return DEFAULT_PERIOD_DAYS
    return max(1, min(int(days), MAX_PERIOD_DAYS))


def _rate(numerator: int, denominator: int) -> float | None:
    """A percentage, or ``None`` when there is nothing to divide by."""
    if not denominator:
        return None
    return round(100 * numerator / denominator, 1)


def _grouped(column, *filters) -> dict:
    """``{column value: count}`` for one grouped query."""
    query = select(column, func.count())
    if filters:
        query = query.where(*filters)
    rows = db.session.execute(query.group_by(column)).all()
    return {key: count for key, count in rows}


# --------------------------------------------------------------------------
# Per-tenant
# --------------------------------------------------------------------------


def _booking_request_stats(garage_id: uuid.UUID, since: datetime) -> dict:
    by_status = _grouped(
        BookingRequest.status,
        BookingRequest.garage_id == garage_id,
        BookingRequest.created_at >= since,
    )
    approved = by_status.get("APPROVED", 0)
    rejected = by_status.get("REJECTED", 0)
    expired = by_status.get("EXPIRED", 0)
    pending = by_status.get("PENDING", 0)
    reviewed = approved + rejected

    return {
        "total": sum(by_status.values()),
        "pending": pending,
        "approved": approved,
        "rejected": rejected,
        "expired": expired,
        # Of the requests staff actually made a decision on. Expired requests
        # are excluded - nobody decided them, so counting them as rejections
        # would blame staff for a queue they never saw.
        "approval_rate": _rate(approved, reviewed),
        "rejection_rate": _rate(rejected, reviewed),
        # Of everything received: how much was left to time out.
        "expiry_rate": _rate(expired, sum(by_status.values())),
    }


def _appointment_stats(garage_id: uuid.UUID, since: datetime) -> dict:
    by_status = _grouped(
        Appointment.status,
        Appointment.garage_id == garage_id,
        Appointment.start_time >= since,
    )
    total = sum(by_status.values())
    completed = by_status.get("COMPLETED", 0)
    no_show = by_status.get("NO_SHOW", 0)
    cancelled = by_status.get("CANCELLED", 0)
    # Appointments whose outcome is known. An upcoming BOOKED slot isn't a
    # missed one, so it must not drag the no-show rate down.
    concluded = completed + no_show + cancelled

    return {
        "total": total,
        "by_status": by_status,
        "completed": completed,
        "no_show": no_show,
        "cancelled": cancelled,
        "upcoming": by_status.get("BOOKED", 0) + by_status.get("REQUESTED", 0),
        "completion_rate": _rate(completed, concluded),
        "no_show_rate": _rate(no_show, completed + no_show),
        "cancellation_rate": _rate(cancelled, concluded),
    }


def _customer_stats(garage_id: uuid.UUID, since: datetime) -> dict:
    total = db.session.scalar(
        select(func.count()).select_from(Customer).where(Customer.garage_id == garage_id)
    )
    new_in_period = db.session.scalar(
        select(func.count())
        .select_from(Customer)
        .where(Customer.garage_id == garage_id, Customer.created_at >= since)
    )
    # A repeat customer is one with more than one appointment, ever - the
    # measure the garage cares about is loyalty, not activity in a window.
    per_customer = (
        select(Appointment.customer_id, func.count().label("visits"))
        .where(Appointment.garage_id == garage_id)
        .group_by(Appointment.customer_id)
        .subquery()
    )
    with_appointments = db.session.scalar(select(func.count()).select_from(per_customer))
    repeat = db.session.scalar(
        select(func.count()).select_from(per_customer).where(per_customer.c.visits > 1)
    )
    vehicles = db.session.scalar(
        select(func.count()).select_from(Vehicle).where(Vehicle.garage_id == garage_id)
    )

    return {
        "total": total or 0,
        "new_in_period": new_in_period or 0,
        "with_appointments": with_appointments or 0,
        "repeat_customers": repeat or 0,
        "repeat_rate": _rate(repeat or 0, with_appointments or 0),
        "vehicles": vehicles or 0,
    }


def _checklist_stats(garage_id: uuid.UUID, since: datetime) -> dict:
    started = db.session.scalar(
        select(func.count())
        .select_from(AppointmentChecklist)
        .where(
            AppointmentChecklist.garage_id == garage_id,
            AppointmentChecklist.created_at >= since,
        )
    )

    checked = case((AppointmentChecklistItem.status != "NOT_CHECKED", 1), else_=0)
    # Labelled `item_count`, not `items`: `subquery().c.items` would resolve to
    # ColumnCollection.items - the dict method - rather than the column, and
    # SQLAlchemy would then try to bind a bound method as a parameter.
    per_checklist = (
        select(
            AppointmentChecklistItem.appointment_checklist_id.label("checklist_id"),
            func.count().label("item_count"),
            func.sum(checked).label("checked_count"),
        )
        .join(
            AppointmentChecklist,
            AppointmentChecklist.id == AppointmentChecklistItem.appointment_checklist_id,
        )
        .where(
            AppointmentChecklist.garage_id == garage_id,
            AppointmentChecklist.created_at >= since,
        )
        .group_by(AppointmentChecklistItem.appointment_checklist_id)
        .subquery()
    )

    totals = db.session.execute(
        select(
            func.coalesce(func.sum(per_checklist.c.item_count), 0),
            func.coalesce(func.sum(per_checklist.c.checked_count), 0),
            func.count().filter(per_checklist.c.item_count == per_checklist.c.checked_count),
        )
    ).one()
    items, checked_items, fully_complete = totals

    return {
        "started": started or 0,
        "items": int(items or 0),
        "items_completed": int(checked_items or 0),
        "fully_completed": int(fully_complete or 0),
        "item_completion_rate": _rate(int(checked_items or 0), int(items or 0)),
        "checklist_completion_rate": _rate(int(fully_complete or 0), started or 0),
    }


def _reminder_stats(garage_id: uuid.UUID, since: datetime) -> dict:
    by_status = _grouped(
        Reminder.status,
        Reminder.garage_id == garage_id,
        Reminder.type == "MOT",
        Reminder.created_at >= since,
    )
    by_stage = _grouped(
        Reminder.stage,
        Reminder.garage_id == garage_id,
        Reminder.type == "MOT",
        Reminder.created_at >= since,
    )
    sent = by_status.get("SENT", 0)

    # Conversion: of the vehicles reminded in this period, how many were then
    # booked in within the conversion window. Measured per vehicle, not per
    # reminder, so a three-stage sequence that lands one booking counts once.
    reminded = (
        select(
            Reminder.vehicle_id.label("vehicle_id"),
            func.min(Reminder.sent_at).label("first_sent_at"),
        )
        .where(
            Reminder.garage_id == garage_id,
            Reminder.type == "MOT",
            Reminder.created_at >= since,
            Reminder.status == "SENT",
            Reminder.sent_at.isnot(None),
        )
        .group_by(Reminder.vehicle_id)
        .subquery()
    )
    reminded_vehicles = db.session.scalar(select(func.count()).select_from(reminded)) or 0

    converted = (
        db.session.scalar(
            select(func.count(func.distinct(reminded.c.vehicle_id)))
            .select_from(reminded)
            .join(Appointment, Appointment.vehicle_id == reminded.c.vehicle_id)
            .where(
                Appointment.garage_id == garage_id,
                Appointment.created_at >= reminded.c.first_sent_at,
                Appointment.created_at
                <= reminded.c.first_sent_at + timedelta(days=REMINDER_CONVERSION_WINDOW_DAYS),
                Appointment.status.notin_(("CANCELLED", "NO_SHOW")),
            )
        )
        or 0
    )

    return {
        "total": sum(by_status.values()),
        "sent": sent,
        "failed": by_status.get("FAILED", 0),
        "skipped": by_status.get("SKIPPED", 0),
        "pending": by_status.get("PENDING", 0),
        "by_stage": {stage or "UNKNOWN": count for stage, count in by_stage.items()},
        "vehicles_reminded": reminded_vehicles,
        "vehicles_booked_after_reminder": converted,
        "conversion_rate": _rate(converted, reminded_vehicles),
        "conversion_window_days": REMINDER_CONVERSION_WINDOW_DAYS,
    }


def _communication_stats(garage_id: uuid.UUID | None, since: datetime) -> dict:
    """Per-channel volume and delivery outcome. ``garage_id=None`` aggregates
    the whole platform."""
    filters = [CommunicationLog.created_at >= since]
    if garage_id is not None:
        filters.append(CommunicationLog.garage_id == garage_id)

    rows = db.session.execute(
        select(
            CommunicationLog.channel,
            CommunicationLog.direction,
            CommunicationLog.status,
            func.count(),
        )
        .where(*filters)
        .group_by(CommunicationLog.channel, CommunicationLog.direction, CommunicationLog.status)
    ).all()

    channels: dict[str, dict] = {}
    for channel, direction, status, count in rows:
        bucket = channels.setdefault(
            channel,
            {
                "total": 0,
                "inbound": 0,
                "outbound": 0,
                "system": 0,
                "delivered": 0,
                "failed": 0,
                "skipped_not_configured": 0,
                "other": 0,
            },
        )
        bucket["total"] += count
        bucket[{"INBOUND": "inbound", "OUTBOUND": "outbound"}.get(direction, "system")] += count

        if status in FAILED_STATUSES:
            bucket["failed"] += count
        elif status in SKIPPED_STATUSES:
            bucket["skipped_not_configured"] += count
        elif status in DELIVERED_STATUSES:
            bucket["delivered"] += count
        else:
            bucket["other"] += count

    for bucket in channels.values():
        # Only attempts that were really handed to a provider can have a
        # delivery rate - a SKIPPED row never left the building.
        attempted = bucket["delivered"] + bucket["failed"]
        bucket["delivery_rate"] = _rate(bucket["delivered"], attempted)
        bucket["failure_rate"] = _rate(bucket["failed"], attempted)

    totals = {
        "total": sum(b["total"] for b in channels.values()),
        "delivered": sum(b["delivered"] for b in channels.values()),
        "failed": sum(b["failed"] for b in channels.values()),
        "skipped_not_configured": sum(b["skipped_not_configured"] for b in channels.values()),
        "inbound": sum(b["inbound"] for b in channels.values()),
        "outbound": sum(b["outbound"] for b in channels.values()),
    }
    totals["delivery_rate"] = _rate(totals["delivered"], totals["delivered"] + totals["failed"])
    totals["failure_rate"] = _rate(totals["failed"], totals["delivered"] + totals["failed"])

    return {"by_channel": channels, "totals": totals}


def tenant_stats(
    garage: Garage, *, days: int = DEFAULT_PERIOD_DAYS, now: datetime | None = None
) -> dict:
    """Everything the per-business statistics page shows."""
    now = now or _utcnow()
    days = clamp_days(days)
    since = now - timedelta(days=days)

    return {
        "garage_id": garage.id,
        "period_days": days,
        "period_start": since,
        "period_end": now,
        "booking_requests": _booking_request_stats(garage.id, since),
        "appointments": _appointment_stats(garage.id, since),
        "customers": _customer_stats(garage.id, since),
        "checklists": _checklist_stats(garage.id, since),
        "mot_reminders": _reminder_stats(garage.id, since),
        "communications": _communication_stats(garage.id, since),
    }


# --------------------------------------------------------------------------
# Platform-wide
# --------------------------------------------------------------------------


def platform_overview(
    *, days: int = DEFAULT_PERIOD_DAYS, dormant_days: int = 30, now: datetime | None = None
) -> dict:
    """Tenant counts, signups, and aggregate usage across every business."""
    from .tenants import last_activity_expression

    now = now or _utcnow()
    days = clamp_days(days)
    since = now - timedelta(days=days)
    dormant_cutoff = now - timedelta(days=dormant_days)

    by_status = _grouped(Garage.status)
    by_plan = _grouped(Garage.plan)
    total = sum(by_status.values())

    dormant = db.session.scalar(
        select(func.count())
        .select_from(Garage)
        .where(
            Garage.status != GARAGE_STATUS_SUSPENDED, last_activity_expression() < dormant_cutoff
        )
    )

    signups_in_period = db.session.scalar(
        select(func.count()).select_from(Garage).where(Garage.created_at >= since)
    )
    signups_previous_period = db.session.scalar(
        select(func.count())
        .select_from(Garage)
        .where(Garage.created_at >= since - timedelta(days=days), Garage.created_at < since)
    )

    trials_ending = db.session.scalar(
        select(func.count())
        .select_from(Garage)
        .where(
            Garage.status == GARAGE_STATUS_TRIAL,
            Garage.trial_ends_at.isnot(None),
            Garage.trial_ends_at <= now + timedelta(days=14),
        )
    )

    def _count(model, *filters):
        return db.session.scalar(select(func.count()).select_from(model).where(*filters)) or 0

    return {
        "period_days": days,
        "period_start": since,
        "period_end": now,
        "tenants": {
            "total": total,
            "active": by_status.get(GARAGE_STATUS_ACTIVE, 0),
            "trial": by_status.get(GARAGE_STATUS_TRIAL, 0),
            "suspended": by_status.get(GARAGE_STATUS_SUSPENDED, 0),
            "dormant": dormant or 0,
            "by_plan": by_plan,
            "trials_ending_soon": trials_ending or 0,
        },
        "signups": {
            "in_period": signups_in_period or 0,
            "previous_period": signups_previous_period or 0,
            "change_percent": _rate(
                (signups_in_period or 0) - (signups_previous_period or 0),
                signups_previous_period or 0,
            ),
            "all_time": total,
        },
        "usage": {
            "booking_requests": _count(BookingRequest, BookingRequest.created_at >= since),
            "appointments": _count(Appointment, Appointment.created_at >= since),
            "customers": _count(Customer, Customer.created_at >= since),
            "vehicles": _count(Vehicle, Vehicle.created_at >= since),
            "customers_total": _count(Customer),
            "vehicles_total": _count(Vehicle),
            "appointments_total": _count(Appointment),
        },
        "communications": _communication_stats(None, since),
        # Deliberately empty until prices live in this system - see the module
        # docstring. The plan breakdown above is the hook.
        "revenue": None,
    }


def signup_growth(*, days: int = 90, now: datetime | None = None) -> dict:
    """Signups per day over the window, with a running total.

    Buckets with no signups are filled in, so the chart has a continuous
    x-axis instead of skipping quiet days.
    """
    now = now or _utcnow()
    days = clamp_days(days)
    start_date = (now - timedelta(days=days - 1)).date()

    rows = db.session.execute(
        select(cast(Garage.created_at, Date).label("day"), func.count())
        .where(Garage.created_at >= datetime.combine(start_date, datetime.min.time(), tzinfo=UTC))
        .group_by("day")
        .order_by("day")
    ).all()
    per_day: dict[date, int] = {day: count for day, count in rows}

    before_window = (
        db.session.scalar(
            select(func.count())
            .select_from(Garage)
            .where(
                Garage.created_at < datetime.combine(start_date, datetime.min.time(), tzinfo=UTC)
            )
        )
        or 0
    )

    points = []
    running = before_window
    for offset in range(days):
        day = start_date + timedelta(days=offset)
        count = per_day.get(day, 0)
        running += count
        points.append({"date": day, "signups": count, "cumulative": running})

    return {"period_days": days, "points": points}
