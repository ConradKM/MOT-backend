"""RFC 5545 serialisation of appointments, via the maintained ``icalendar``
package (line folding, text escaping and CRLF endings are its job, not ours).

Events are deliberately minimal and the same for every export: the service
and vehicle registration, start/end, the garage's address, the worker's name
when more than one worker is included, and a link back to the appointment in
the app (which requires a login). Never a customer's name, phone, email or
the appointment's notes - calendars sync to personal phones, and a
subscription URL is a bearer secret.

Times are emitted in UTC (``...Z``): no VTIMEZONE blocks, no DST edge cases,
and every client renders them in its own local time. DTSTAMP, LAST-MODIFIED
and SEQUENCE are all derived from stored timestamps rather than "now", so the
same data always serialises to the same bytes - which is what lets a feed
answer a conditional request with a 304.
"""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from flask import current_app
from icalendar import Calendar, Event

from app.models.appointments.appointment import Appointment
from app.models.employee import Employee
from app.models.garage import Garage

PRODID = "-//CoMaz OS//Appointments//EN"
FEED_REFRESH_INTERVAL = timedelta(hours=1)


def _utc(value: datetime) -> datetime:
    # Persisted columns are timezone-aware; normalise to UTC so the output is
    # always the ...Z form.
    return value.astimezone(UTC)


def garage_location(garage: Garage) -> str | None:
    parts = [garage.address, garage.address_city, garage.address_region, garage.postcode]
    location = ", ".join(part.strip() for part in parts if part and part.strip())
    return location or None


def worker_name(employee: Employee) -> str:
    # The staff app's employeeDisplayName falls back to the email address;
    # an event title syncs to personal phones, so fall back to a neutral label.
    name = " ".join(part for part in (employee.first_name, employee.last_name) if part).strip()
    return name or "Unnamed worker"


def event_summary(appointment: Appointment, *, show_worker: bool) -> str:
    service = appointment.appointment_type_name_at_booking or appointment.appointment_type.name
    summary = service
    if appointment.vehicle is not None and appointment.vehicle.registration_number:
        summary = f"{summary} - {appointment.vehicle.registration_number}"
    if show_worker:
        summary = f"{summary} ({worker_name(appointment.employee)})"
    return summary


def content_modified_at(appointment: Appointment, *, show_worker: bool) -> datetime:
    """When anything shown in this event last changed.

    The vehicle registration, garage address and worker name live on other
    rows, so an edit to them must also advance LAST-MODIFIED/SEQUENCE or an
    importing client would keep the stale title. Each input only ever moves
    forward, so the max does too.
    """
    stamps = [appointment.updated_at, appointment.garage.updated_at]
    if appointment.vehicle is not None:
        stamps.append(appointment.vehicle.updated_at)
    if show_worker:
        stamps.append(appointment.employee.updated_at)
    return max(_utc(stamp) for stamp in stamps)


def event_sequence(appointment: Appointment, modified_at: datetime) -> int:
    """Seconds between creation and the last visible change: 0 for an
    untouched appointment and strictly larger after every edit, so clients
    update the event in place rather than duplicating it."""
    return max(0, int((modified_at - _utc(appointment.created_at)).total_seconds()))


def appointment_url(appointment: Appointment) -> str:
    base = str(current_app.config["APP_BASE_URL"]).rstrip("/")
    return f"{base}/{appointment.garage_id}/appointments/{appointment.id}/overview"


def build_event(appointment: Appointment, *, show_worker: bool, location: str | None) -> Event:
    modified_at = content_modified_at(appointment, show_worker=show_worker)

    event = Event()
    event.add("uid", f"{appointment.id}@{current_app.config['CALENDAR_UID_DOMAIN']}")
    event.add("dtstamp", modified_at)
    event.add("last-modified", modified_at)
    event.add("sequence", event_sequence(appointment, modified_at))
    event.add("dtstart", _utc(appointment.start_time))
    event.add("dtend", _utc(appointment.end_time))
    event.add("summary", event_summary(appointment, show_worker=show_worker))
    if location:
        event.add("location", location)
    event.add("url", appointment_url(appointment))
    event.add("status", "CONFIRMED")
    event.add("transp", "OPAQUE")
    return event


def build_calendar(
    garage: Garage,
    appointments: Sequence[Appointment],
    *,
    show_worker: bool,
    calendar_name: str,
    is_feed: bool,
) -> bytes:
    calendar = Calendar()
    calendar.add("prodid", PRODID)
    calendar.add("version", "2.0")
    calendar.add("calscale", "GREGORIAN")
    calendar.add("x-wr-calname", calendar_name)
    if is_feed:
        # How often subscribers should re-poll: REFRESH-INTERVAL is the
        # RFC 7986 property, X-PUBLISHED-TTL the older one Apple and Outlook
        # still read. Google ignores both and refreshes on its own schedule.
        calendar.add("refresh-interval", FEED_REFRESH_INTERVAL, parameters={"VALUE": "DURATION"})
        calendar.add("x-published-ttl", "PT1H")

    location = garage_location(garage)
    for appointment in appointments:
        calendar.add_component(build_event(appointment, show_worker=show_worker, location=location))

    body: bytes = calendar.to_ical()
    return body
