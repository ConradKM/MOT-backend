"""Slots describe the whole appointment, not just its start.

Every slot carries the ``end`` and ``duration_minutes`` the customer is
actually booking, a start is only offered when that whole span is free, and
"free" means peak concurrency stays under capacity - not that nothing at all
overlaps it.
"""

import datetime
from types import SimpleNamespace

from app.models.booking_request import BookingRequest
from app.models.garage_schedule import GarageScheduleException
from app.models.queueing.reserved_window import WalkInReservedWindow
from app.public_booking.availability import (
    day_slots,
    resolve_opening_hours,
    resolve_settings,
    slot_capacity_usage,
)

UTC = datetime.UTC


def _future_weekday(min_days=5):
    d = datetime.datetime.now(UTC).date() + datetime.timedelta(days=min_days)
    while d.weekday() >= 5:
        d += datetime.timedelta(days=1)
    return d


def _at(day, hour, minute=0):
    return datetime.datetime.combine(day, datetime.time(hour, minute), tzinfo=UTC)


def _type(session, garage, minutes, name=None):
    from app.models.appointments.appointment_type import GarageAppointmentType

    t = GarageAppointmentType(
        garage_id=garage.id,
        name=name or f"{minutes} minute job",
        status="ACTIVE",
        default_duration_minutes=minutes,
    )
    session.add(t)
    session.commit()
    return t


def _slots(client, garage, day, **query):
    body = client.get(
        f"/api/public/{garage.slug}/availability/{day.isoformat()}", query_string=query
    ).get_json()
    return body, {s["start"]: s for s in body["slots"]}


def _open(session, garage, day, opens, closes):
    session.add(
        GarageScheduleException(
            garage_id=garage.id,
            date=day,
            is_closed=False,
            opens_at=datetime.time(opens),
            closes_at=datetime.time(closes),
        )
    )
    session.commit()


def _request(session, garage, day, hour, minutes, status="PENDING", **extra):
    br = BookingRequest(
        garage_id=garage.id,
        status=status,
        customer_first_name="Sam",
        customer_last_name="Lee",
        customer_email="sam.lee@example.com",
        vehicle_registration="PB11 AAA",
        preferred_date=day,
        preferred_time=datetime.time(hour),
        requested_duration_minutes=minutes,
        **extra,
    )
    session.add(br)
    session.commit()
    return br


# --------------------------------------------------------------------------
# end / duration_minutes on the payload
# --------------------------------------------------------------------------


def test_thirty_minute_slots_report_their_end(client, session, garage):
    day = _future_weekday()
    t = _type(session, garage, 30)

    body, slots = _slots(client, garage, day, appointment_type_id=str(t.id))

    assert body["duration_minutes"] == 30
    assert slots["10:00"]["end"] == "10:30"
    assert slots["10:00"]["duration_minutes"] == 30
    assert set(slots["10:00"]) == {
        "start",
        "end",
        "duration_minutes",
        "status",
        "remaining",
        "capacity",
    }


def test_four_hour_slots_span_four_hours_and_stop_in_time_to_finish(client, session, garage):
    day = _future_weekday()
    _open(session, garage, day, 9, 18)
    t = _type(session, garage, 240)

    body, slots = _slots(client, garage, day, appointment_type_id=str(t.id))

    assert body["duration_minutes"] == 240
    assert slots["10:00"]["end"] == "14:00"
    # Closes 18:00, so a 4-hour job's latest start is 14:00.
    assert body["slots"][-1]["start"] == "14:00"
    assert body["slots"][-1]["end"] == "18:00"
    assert "14:05" not in slots


def test_add_ons_extend_the_reported_span(client, session, garage):
    from app.models.appointments.add_on import AddOn

    day = _future_weekday()
    t = _type(session, garage, 60)
    add_on = AddOn(
        garage_id=garage.id,
        appointment_type_id=t.id,
        name="Wheel alignment",
        price_delta="20.00",
        duration_delta_minutes=45,
        max_quantity=1,
    )
    session.add(add_on)
    session.commit()

    body, slots = _slots(client, garage, day, appointment_type_id=str(t.id), add_ons=str(add_on.id))

    assert body["duration_minutes"] == 105
    assert slots["10:00"]["end"] == "11:45"
    assert slots["10:00"]["duration_minutes"] == 105


def test_no_service_chosen_reports_the_generic_length(client, garage):
    body, slots = _slots(client, garage, _future_weekday())

    assert body["duration_minutes"] == 60
    assert slots["09:00"]["end"] == "10:00"


# --------------------------------------------------------------------------
# A start is only free when its whole span is
# --------------------------------------------------------------------------


def test_four_hour_job_around_a_midday_booking(client, session, garage, make_appointment):
    day = _future_weekday()
    _open(session, garage, day, 7, 18)
    make_appointment(_at(day, 12), minutes=60)
    t = _type(session, garage, 240)

    _, slots = _slots(client, garage, day, appointment_type_id=str(t.id))

    # Spans that end by 12:00 (end == next start is not an overlap).
    assert slots["07:00"]["status"] == "available"
    assert slots["08:00"]["status"] == "available"
    assert slots["08:00"]["end"] == "12:00"
    # Every start whose span reaches into 12:00-13:00 is taken.
    for hh in range(8, 13):
        for mm in range(0, 60, 5):
            start = f"{hh:02d}:{mm:02d}"
            if "08:05" <= start <= "12:55":
                assert slots[start]["status"] == "booked", start
    # From 13:00 the span starts after the booking ends.
    assert slots["13:00"]["status"] == "available"
    assert slots["14:00"]["status"] == "available"


def test_lead_time_cutoff_hides_starts_before_it(session, garage):
    day = _future_weekday()
    settings = resolve_settings(garage)  # default 2h lead time
    now = _at(day, 8)

    slots = day_slots(
        garage, day, settings, resolve_opening_hours(garage), {}, now, duration_min=240
    )

    assert slots[0]["start"] == "10:00"
    assert slots[0]["end"] == "14:00"


def test_capacity_two_counts_peak_concurrency_not_overlaps(
    client, session, garage, garage_schedule, make_appointment
):
    garage_schedule.capacity_per_slot = 2
    session.commit()
    day = _future_weekday()
    make_appointment(_at(day, 10), minutes=60)
    make_appointment(_at(day, 12), minutes=60)
    t = _type(session, garage, 240)

    _, slots = _slots(client, garage, day, appointment_type_id=str(t.id))

    # 10:00-14:00 touches both bookings, but never more than one at once -
    # a second bay is free for the whole span.
    assert slots["10:00"]["status"] != "booked"
    assert slots["10:00"]["remaining"] == 1

    # Two genuinely simultaneous bookings do fill both bays.
    make_appointment(_at(day, 10, 30), minutes=60)
    _, slots = _slots(client, garage, day, appointment_type_id=str(t.id))
    assert slots["10:00"]["status"] == "booked"


def test_staff_capacity_check_uses_the_same_peak_accounting(
    session, garage, garage_schedule, make_appointment
):
    garage_schedule.capacity_per_slot = 2
    session.commit()
    day = _future_weekday()
    make_appointment(_at(day, 10), minutes=60)
    make_appointment(_at(day, 12), minutes=60)

    used, capacity = slot_capacity_usage(garage, day, _at(day, 10), 240)

    assert (used, capacity) == (1, 2)


def test_pending_request_and_deposit_hold_reserve_their_spans(client, session, garage):
    day = _future_weekday()
    _request(session, garage, day, 10, 120)
    _request(
        session,
        garage,
        day,
        14,
        60,
        status="AWAITING_PAYMENT",
        payment_hold_expires_at=datetime.datetime.now(UTC) + datetime.timedelta(minutes=10),
    )

    _, slots = _slots(client, garage, day)

    assert slots["11:55"]["status"] == "booked"  # inside the 10:00-12:00 request
    assert slots["12:00"]["status"] == "available"
    assert slots["13:00"]["status"] == "available"  # 13:00-14:00 touches the hold
    assert slots["13:05"]["status"] == "booked"
    assert slots["14:55"]["status"] == "booked"
    assert slots["15:00"]["status"] == "available"


def test_walk_in_window_still_withholds_a_bay(
    client, session, garage, garage_schedule, make_appointment
):
    garage_schedule.capacity_per_slot = 2
    session.add(
        WalkInReservedWindow(
            garage_id=garage.id,
            weekday=_future_weekday().weekday(),
            starts_at=datetime.time(15),
            ends_at=datetime.time(16),
            reserved_capacity=1,
        )
    )
    session.commit()
    day = _future_weekday()
    make_appointment(_at(day, 12), minutes=60)
    t = _type(session, garage, 240)

    _, slots = _slots(client, garage, day, appointment_type_id=str(t.id))

    # 09:00-13:00: one booking, no window -> one bay left.
    assert slots["09:00"]["remaining"] == 1
    # 12:00-16:00: one booking plus the reserved walk-in bay -> full.
    assert slots["12:00"]["status"] == "booked"


def test_service_without_a_duration_uses_the_garage_default(
    client, session, garage, garage_schedule
):
    garage_schedule.default_appointment_minutes = 45
    session.commit()
    from app.models.appointments.appointment_type import GarageAppointmentType

    t = GarageAppointmentType(garage_id=garage.id, name="Diagnostic", status="ACTIVE")
    session.add(t)
    session.commit()

    public = client.get(f"/api/public/{garage.slug}").get_json()
    [listed] = [x for x in public["appointment_types"] if x["id"] == str(t.id)]
    assert listed["default_duration_minutes"] is None
    assert listed["effective_duration_minutes"] == 45

    day = _future_weekday()
    body, slots = _slots(client, garage, day, appointment_type_id=str(t.id))
    assert body["duration_minutes"] == 45
    assert slots["09:30"]["end"] == "10:15"


def test_service_with_a_duration_reports_it_as_effective(client, session, garage):
    t = _type(session, garage, 240)

    public = client.get(f"/api/public/{garage.slug}").get_json()
    [listed] = [x for x in public["appointment_types"] if x["id"] == str(t.id)]
    assert listed["effective_duration_minutes"] == 240


def test_submit_rejects_a_span_that_has_since_been_taken(client, session, garage, make_appointment):
    day = _future_weekday()
    t = _type(session, garage, 240)
    _, slots = _slots(client, garage, day, appointment_type_id=str(t.id))
    assert slots["10:00"]["status"] == "available"

    # Someone books the middle of that span before the customer submits.
    make_appointment(_at(day, 12), minutes=60)

    resp = client.post(
        f"/api/public/{garage.slug}/booking-requests",
        json={
            "customer_first_name": "Alex",
            "customer_last_name": "Turner",
            "customer_email": "alex.turner@example.com",
            "customer_phone": "07123 456789",
            "vehicle_registration": "PB11 REQ",
            "preferred_date": day.isoformat(),
            "preferred_time": "10:00:00",
            "appointment_type_id": str(t.id),
        },
    )
    assert resp.status_code == 409


def test_end_reflects_a_clock_change_mid_appointment(session, garage):
    """UK clocks go forward at 01:00 on 28 March 2027: four real hours from
    00:00 GMT finish at 05:00 BST."""
    garage.timezone = "Europe/London"
    session.commit()
    day = datetime.date(2027, 3, 28)
    exceptions = {
        day: SimpleNamespace(is_closed=False, opens_at=datetime.time(0), closes_at=datetime.time(8))
    }

    slots = day_slots(
        garage,
        day,
        resolve_settings(garage),
        resolve_opening_hours(garage),
        exceptions,
        _at(day - datetime.timedelta(days=7), 0),
        duration_min=240,
    )

    by_start = {s["start"]: s for s in slots}
    assert by_start["00:00"]["end"] == "05:00"
    assert by_start["03:00"]["end"] == "07:00"
    assert all(s["duration_minutes"] == 240 for s in slots)
