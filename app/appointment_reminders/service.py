"""Appointment reminder scheduling + delivery.

Mirrors the shape of ``app.mot_reminders.service`` (single delivery choke
point, idempotent automatic worker) but generalised: any number of
owner-configured lead times, and a real choice of channel via the existing
``app.communications`` provider layer instead of email-only.

Key correctness property for rescheduling: the "is this due, and have we
already sent it" check is always computed from the appointment's *current*
``start_time`` (never a value cached at settings-save time), and a sent
reminder is matched by ``(appointment_id, type, stage, scheduled_at)`` where
``scheduled_at`` is set to the due instant (``start_time - hours_before``).
Rescheduling an appointment changes that due instant, so:
  * a reminder already sent for the old time is never re-sent (it's history,
    tied to a due instant that no longer matters), and
  * a fresh reminder is correctly considered "not yet sent" for the new time,
    because the (stage, scheduled_at) pair differs.
Nothing here ever re-fires a reminder for a stale start_time, because the
worker only ever reads the appointment's live start_time.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.communications.events import APPOINTMENT_REMINDER_DUE, emit_event
from app.communications.service import send_sms_message, send_whatsapp_message
from app.email import send_email
from app.extensions import db
from app.models.appointments.appointment import Appointment
from app.models.reminder import (
    STATUS_FAILED,
    STATUS_SENT,
    STATUS_SKIPPED,
    TRIGGER_AUTOMATIC,
    Reminder,
)

from .defaults import (
    CHANNEL_EMAIL,
    CHANNEL_SMS,
    CHANNEL_WHATSAPP,
    VALID_CHANNELS,
    resolve_appointment_reminder_settings,
)

REMINDER_TYPE = "APPOINTMENT"

# Only a confirmed, still-upcoming appointment gets reminded about. REQUESTED
# isn't confirmed yet; CANCELLED/NO_SHOW/COMPLETED/ACTION_NEEDED are terminal
# or already-happened as far as a reminder is concerned.
REMINDABLE_APPOINTMENT_STATUSES = ("BOOKED",)

_UK_TZ = ZoneInfo("Europe/London")


def _stage_for(hours_before: int) -> str:
    return f"H{hours_before}"


def format_uk_date(dt: datetime) -> str:
    """UK-facing date, e.g. '22 September 2026'. ``dt`` must be tz-aware.

    Avoids the platform-specific ``%-d``/``%#d`` strftime flags (glibc-only,
    not available on Windows) so this works the same in dev and prod.
    """
    local = dt.astimezone(_UK_TZ)
    return f"{local.day} {local.strftime('%B %Y')}"


def format_uk_time(dt: datetime) -> str:
    """UK-facing time, e.g. '3:00pm'. ``dt`` must be tz-aware."""
    local = dt.astimezone(_UK_TZ)
    hour12 = local.hour % 12 or 12
    period = "am" if local.hour < 12 else "pm"
    return f"{hour12}:{local.minute:02d}{period}"


def _reminder_body(*, garage, customer, appointment: Appointment) -> str:
    name = customer.first_name or "there"
    service_name = appointment.appointment_type.name if appointment.appointment_type else "your appointment"
    return (
        f"Hi {name},\n\n"
        f"This is a reminder that you have {service_name} booked with {garage.name} "
        f"on {format_uk_date(appointment.start_time)} at {format_uk_time(appointment.start_time)}.\n\n"
        f"{garage.name}"
    )


def available_channels(garage) -> list[str]:
    """Channels this garage can actually send on right now. Email is always
    available (it needs no per-tenant setup); SMS/WhatsApp only appear once
    Twilio is configured for this garage - never claim a channel works when
    it doesn't."""
    from app.communications.providers import get_messaging_provider, get_sms_provider

    channels = [CHANNEL_EMAIL]
    if get_sms_provider(garage).configuration_error(garage) is None:
        channels.append(CHANNEL_SMS)
    if get_messaging_provider(garage).configuration_error(garage) is None:
        channels.append(CHANNEL_WHATSAPP)
    return channels


def _pick_channel(*, garage, customer, settings) -> str | None:
    """The first channel, in the owner's configured priority order, that is
    both actually available for this garage and has the contact info it
    needs on this customer. ``None`` if nothing usable."""
    usable = set(available_channels(garage))
    for channel in settings.channels:
        if channel not in VALID_CHANNELS or channel not in usable:
            continue
        if channel == CHANNEL_EMAIL and customer.email:
            return channel  # type: ignore[no-any-return]
        if channel in (CHANNEL_SMS, CHANNEL_WHATSAPP) and customer.phone:
            return channel  # type: ignore[no-any-return]
    return None


def deliver_reminder(*, garage, customer, appointment: Appointment, channel: str):
    """Send the reminder over ``channel``. Returns ``(status, detail, provider_message_id)``."""
    body = _reminder_body(garage=garage, customer=customer, appointment=appointment)

    if channel == CHANNEL_EMAIL:
        if not customer.email:
            return STATUS_SKIPPED, "No email address on file for this customer.", None
        send_email(
            to=customer.email,
            subject=f"Appointment reminder - {garage.name}",
            body=body,
        )
        return STATUS_SENT, f"Emailed {customer.email}.", None

    if channel == CHANNEL_SMS:
        if not customer.phone:
            return STATUS_SKIPPED, "No phone number on file for this customer.", None
        log = send_sms_message(
            garage=garage,
            to=customer.phone,
            body=body,
            customer=customer,
            appointment=appointment,
            trigger_event=APPOINTMENT_REMINDER_DUE,
        )
        return _status_from_log(log), (log.error_message or f"SMS to {log.to_address}."), log.external_id

    if channel == CHANNEL_WHATSAPP:
        if not customer.phone:
            return STATUS_SKIPPED, "No phone number on file for this customer.", None
        log = send_whatsapp_message(
            garage=garage,
            to=customer.phone,
            body=body,
            customer=customer,
            appointment=appointment,
            trigger_event=APPOINTMENT_REMINDER_DUE,
        )
        return _status_from_log(log), (log.error_message or f"WhatsApp to {log.to_address}."), log.external_id

    return STATUS_SKIPPED, f"The {channel!r} channel is not available.", None


def _status_from_log(log) -> str:
    if log.status == "SKIPPED_NOT_CONFIGURED":
        return STATUS_SKIPPED
    if log.status == "FAILED":
        return STATUS_FAILED
    # queued/sent/delivered etc - handed off successfully.
    return STATUS_SENT


def record_and_send(
    *,
    garage,
    customer,
    appointment: Appointment,
    hours_before: int,
    due_at: datetime,
    channel: str | None,
    session=None,
    now: datetime | None = None,
) -> Reminder:
    """Deliver one appointment reminder and persist the event row. If
    ``channel`` is ``None`` (nothing usable for this garage/customer), records
    a SKIPPED row rather than not recording anything - the owner should be
    able to see that a reminder was due but couldn't be sent, and why."""
    session = session or db.session
    now = now or datetime.now(UTC)

    if channel is None:
        status, detail, provider_message_id = (
            STATUS_SKIPPED,
            "No usable communication channel configured for this business/customer.",
            None,
        )
        channel = "none"
    else:
        status, detail, provider_message_id = deliver_reminder(
            garage=garage, customer=customer, appointment=appointment, channel=channel
        )

    reminder = Reminder(
        garage_id=garage.id,
        customer_id=customer.id,
        vehicle_id=appointment.vehicle_id,
        appointment_id=appointment.id,
        type=REMINDER_TYPE,
        channel=channel,
        trigger=TRIGGER_AUTOMATIC,
        stage=_stage_for(hours_before),
        mot_expiry_date=None,
        scheduled_at=due_at,
        sent_at=now if status == STATUS_SENT else None,
        status=status,
        detail=detail,
        provider_message_id=provider_message_id,
    )
    session.add(reminder)
    session.flush()

    emit_event(
        APPOINTMENT_REMINDER_DUE,
        garage=garage,
        customer=customer,
        appointment=appointment,
        reminder=reminder,
    )

    return reminder


def _already_sent(*, appointment_id, hours_before: int, due_at: datetime) -> bool:
    return (
        Reminder.query.filter(
            Reminder.appointment_id == appointment_id,
            Reminder.type == REMINDER_TYPE,
            Reminder.stage == _stage_for(hours_before),
            Reminder.scheduled_at == due_at,
            Reminder.status.in_((STATUS_SENT, STATUS_SKIPPED)),
        ).first()
        is not None
    )


def send_due_appointment_reminders(
    *, session=None, garage_id=None, now: datetime | None = None
) -> list[Reminder]:
    """Send every enabled reminder timing that is due for every valid,
    upcoming appointment, and hasn't already been sent for the appointment's
    *current* scheduled time. Idempotent and reschedule-safe - safe to run as
    often as you like (see module docstring)."""
    session = session or db.session
    now = now or datetime.now(UTC)

    q = Appointment.query.filter(
        Appointment.status.in_(REMINDABLE_APPOINTMENT_STATUSES),
        Appointment.start_time > now,
    )
    if garage_id is not None:
        q = q.filter(Appointment.garage_id == garage_id)
    appointments = q.all()

    created: list[Reminder] = []
    settings_cache: dict = {}

    for appointment in appointments:
        settings = settings_cache.setdefault(
            appointment.garage_id,
            resolve_appointment_reminder_settings(appointment.garage_id, session),
        )
        if not settings.enabled:
            continue

        garage = appointment.garage
        customer = appointment.customer
        if customer is None:
            continue

        for timing in settings.timings:
            if not timing.enabled:
                continue
            hours_before = timing.hours_before
            due_at = appointment.start_time - timedelta(hours=hours_before)
            if due_at > now:
                continue  # not due yet
            if _already_sent(appointment_id=appointment.id, hours_before=hours_before, due_at=due_at):
                continue

            channel = _pick_channel(garage=garage, customer=customer, settings=settings)
            created.append(
                record_and_send(
                    garage=garage,
                    customer=customer,
                    appointment=appointment,
                    hours_before=hours_before,
                    due_at=due_at,
                    channel=channel,
                    session=session,
                    now=now,
                )
            )

    if created:
        session.commit()
    return created
