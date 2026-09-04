"""API tests for the public availability calendar.

GET /api/public/<slug>/availability
GET /api/public/<slug>/availability/<date>
"""

import datetime

from app.models.booking_request import BookingRequest
from app.models.garage_schedule import GarageScheduleException

UTC = datetime.UTC


def _future_weekday(min_days=5):
    d = datetime.date.today() + datetime.timedelta(days=min_days)
    while d.weekday() >= 5:  # Sat / Sun
        d += datetime.timedelta(days=1)
    return d


def _future_weekend(min_days=3):
    d = datetime.date.today() + datetime.timedelta(days=min_days)
    while d.weekday() < 5:
        d += datetime.timedelta(days=1)
    return d


def _at(day, hour, minute=0):
    return datetime.datetime.combine(
        day, datetime.time(hour, minute), tzinfo=UTC
    )


# --------------------------------------------------------------------------
# GET /api/public/<slug>/availability
# --------------------------------------------------------------------------


def test_range_shape_with_default_schedule(client, garage):
    body = client.get(f"/api/public/{garage.slug}/availability").get_json()

    assert body["garage"]["slug"] == garage.slug
    assert set(body["rules"]) == {
        "slot_interval_minutes",
        "min_lead_time_hours",
        "max_advance_days",
        "booking_window_start",
        "booking_window_end",
    }
    assert body["rules"]["booking_window_start"] == datetime.date.today().isoformat()

    hours = {h["weekday"]: h for h in body["opening_hours"]}
    assert len(hours) == 7
    assert hours[0]["is_closed"] is False
    assert hours[5]["is_closed"] is True and hours[6]["is_closed"] is True

    assert body["days"][0]["date"] == datetime.date.today().isoformat()
    assert {d["level"] for d in body["days"]} <= {
        "available",
        "limited",
        "full",
        "closed",
        "past",
    }


def test_range_respects_from_and_to(client, garage):
    start = datetime.date.today() + datetime.timedelta(days=3)
    end = start + datetime.timedelta(days=4)
    body = client.get(
        f"/api/public/{garage.slug}/availability",
        query_string={"from": start.isoformat(), "to": end.isoformat()},
    ).get_json()

    dates = [d["date"] for d in body["days"]]
    assert dates[0] == start.isoformat()
    assert dates[-1] == end.isoformat()


def test_range_caps_to_at_booking_window_end(client, garage):
    far = (datetime.date.today() + datetime.timedelta(days=999)).isoformat()
    body = client.get(
        f"/api/public/{garage.slug}/availability", query_string={"to": far}
    ).get_json()

    assert body["days"][-1]["date"] == body["rules"]["booking_window_end"]


def test_range_clamps_past_from_to_today(client, garage):
    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    body = client.get(
        f"/api/public/{garage.slug}/availability", query_string={"from": yesterday}
    ).get_json()

    assert body["days"][0]["date"] == datetime.date.today().isoformat()


def test_weekend_day_is_closed(client, garage):
    weekend = _future_weekend()
    body = client.get(
        f"/api/public/{garage.slug}/availability",
        query_string={"from": weekend.isoformat(), "to": weekend.isoformat()},
    ).get_json()

    assert body["days"][0]["is_open"] is False
    assert body["days"][0]["level"] == "closed"


def test_unknown_slug_returns_404(client):
    assert client.get("/api/public/nope/availability").status_code == 404
    assert (
        client.get("/api/public/nope/availability/2026-09-10").status_code == 404
    )


# --------------------------------------------------------------------------
# GET /api/public/<slug>/availability/<date>
# --------------------------------------------------------------------------


def test_day_slots_listed_for_an_open_weekday(client, garage):
    day = _future_weekday()
    body = client.get(
        f"/api/public/{garage.slug}/availability/{day.isoformat()}"
    ).get_json()

    assert body["is_open"] is True
    # 09:00-17:00, 30-min interval, 60-min duration -> 09:00 .. 16:00 = 15 slots
    assert len(body["slots"]) == 15
    first = body["slots"][0]
    assert first["start"] == "09:00"
    assert set(first) == {"start", "status", "remaining", "capacity"}
    assert {s["status"] for s in body["slots"]} == {"available"}


def test_day_slots_bad_date_returns_422(client, garage):
    resp = client.get(f"/api/public/{garage.slug}/availability/not-a-date")
    assert resp.status_code == 422


def test_booked_appointment_marks_the_slot(client, garage, make_appointment):
    day = _future_weekday()
    make_appointment(_at(day, 10, 0), minutes=60)

    slots = {
        s["start"]: s
        for s in client.get(
            f"/api/public/{garage.slug}/availability/{day.isoformat()}"
        ).get_json()["slots"]
    }

    assert slots["10:00"]["status"] == "booked"
    assert slots["10:00"]["remaining"] == 0
    # 09:00 covers 09:00-10:00 and does not overlap a 10:00 start.
    assert slots["09:00"]["status"] == "available"


def test_cancelled_appointment_does_not_consume_capacity(
    client, garage, make_appointment
):
    day = _future_weekday()
    make_appointment(_at(day, 10, 0), minutes=60, status="CANCELLED")

    slots = {
        s["start"]: s
        for s in client.get(
            f"/api/public/{garage.slug}/availability/{day.isoformat()}"
        ).get_json()["slots"]
    }
    assert slots["10:00"]["status"] == "available"


def test_capacity_and_limited_threshold(
    client, session, garage, garage_schedule, make_appointment
):
    garage_schedule.capacity_per_slot = 3
    garage_schedule.limited_threshold_ratio = 0.5  # floor(3 * .5) = 1
    session.commit()

    day = _future_weekday()
    make_appointment(_at(day, 10, 0), minutes=60)
    make_appointment(_at(day, 10, 0), minutes=60)

    slot = next(
        s
        for s in client.get(
            f"/api/public/{garage.slug}/availability/{day.isoformat()}"
        ).get_json()["slots"]
        if s["start"] == "10:00"
    )
    assert slot["status"] == "limited"
    assert slot["remaining"] == 1

    make_appointment(_at(day, 10, 0), minutes=60)
    slot = next(
        s
        for s in client.get(
            f"/api/public/{garage.slug}/availability/{day.isoformat()}"
        ).get_json()["slots"]
        if s["start"] == "10:00"
    )
    assert slot["status"] == "booked"


def test_pending_booking_request_consumes_capacity(client, session, garage):
    day = _future_weekday()
    br = BookingRequest(
        garage_id=garage.id,
        status="PENDING",
        customer_first_name="Sam",
        customer_last_name="Lee",
        customer_email="sam.lee@example.com",
        vehicle_registration="PB11 AAA",
        preferred_date=day,
        preferred_time=datetime.time(10, 0),
    )
    session.add(br)
    session.commit()

    slots = {
        s["start"]: s
        for s in client.get(
            f"/api/public/{garage.slug}/availability/{day.isoformat()}"
        ).get_json()["slots"]
    }
    assert slots["10:00"]["status"] == "booked"

    # An already-reviewed request no longer holds the slot.
    br.status = "APPROVED"
    session.commit()
    slots = {
        s["start"]: s
        for s in client.get(
            f"/api/public/{garage.slug}/availability/{day.isoformat()}"
        ).get_json()["slots"]
    }
    assert slots["10:00"]["status"] == "available"


def test_lead_time_hides_near_slots(client, session, garage, garage_schedule):
    day = _future_weekday()

    garage_schedule.min_lead_time_hours = 24 * 400  # past the window
    session.commit()
    body = client.get(
        f"/api/public/{garage.slug}/availability/{day.isoformat()}"
    ).get_json()
    assert body["slots"] == []
    assert body["level"] == "full"

    garage_schedule.min_lead_time_hours = 0
    session.commit()
    body = client.get(
        f"/api/public/{garage.slug}/availability/{day.isoformat()}"
    ).get_json()
    assert len(body["slots"]) == 15


def test_schedule_exception_closes_a_day(client, session, garage, garage_schedule):
    day = _future_weekday()
    session.add(
        GarageScheduleException(garage_id=garage.id, date=day, is_closed=True)
    )
    session.commit()

    body = client.get(
        f"/api/public/{garage.slug}/availability/{day.isoformat()}"
    ).get_json()
    assert body["is_open"] is False
    assert body["level"] == "closed"
    assert body["slots"] == []


def _make_type(session, garage, minutes, name="Full Service"):
    from app.models.appointments.appointment_type import GarageAppointmentType

    t = GarageAppointmentType(
        garage_id=garage.id, name=name, status="ACTIVE", default_duration_minutes=minutes
    )
    session.add(t)
    session.commit()
    return t


# --------------------------------------------------------------------------
# Appointment-type-driven duration (item 2/13)
# --------------------------------------------------------------------------


def test_day_slots_use_the_selected_appointment_types_duration(client, session, garage):
    day = _future_weekday()
    ninety_min_type = _make_type(session, garage, 90)

    body = client.get(
        f"/api/public/{garage.slug}/availability/{day.isoformat()}",
        query_string={"appointment_type_id": str(ninety_min_type.id)},
    ).get_json()

    # 09:00-17:00, 30-min interval, 90-min duration -> last start is 15:30
    # (15:30 + 90 = 17:00), not 16:00 as the 60-min default would allow.
    assert body["slots"][-1]["start"] == "15:30"
    assert all(s["start"] != "16:00" for s in body["slots"])


def test_no_type_selected_falls_back_to_the_garages_generic_duration(client, garage):
    day = _future_weekday()
    body = client.get(
        f"/api/public/{garage.slug}/availability/{day.isoformat()}"
    ).get_json()
    assert body["slots"][-1]["start"] == "16:00"


def test_changing_the_selected_type_recalculates_available_times(client, session, garage):
    day = _future_weekday()
    short_type = _make_type(session, garage, 30, "Diagnostic Check")
    long_type = _make_type(session, garage, 90, "Full Service")

    short_slots = client.get(
        f"/api/public/{garage.slug}/availability/{day.isoformat()}",
        query_string={"appointment_type_id": str(short_type.id)},
    ).get_json()["slots"]
    long_slots = client.get(
        f"/api/public/{garage.slug}/availability/{day.isoformat()}",
        query_string={"appointment_type_id": str(long_type.id)},
    ).get_json()["slots"]

    assert len(long_slots) < len(short_slots)


def test_unknown_appointment_type_id_returns_422(client, garage):
    import uuid

    day = _future_weekday()
    resp = client.get(
        f"/api/public/{garage.slug}/availability/{day.isoformat()}",
        query_string={"appointment_type_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 422


def test_a_long_existing_appointment_blocks_every_slot_it_overlaps(
    client, session, garage, make_appointment
):
    day = _future_weekday()
    # 12:00-13:30, so there's room to check a genuinely non-overlapping
    # 90-minute candidate both before (09:00-10:30) and after (13:30-15:00).
    make_appointment(_at(day, 12, 0), minutes=90)
    ninety_min_type = _make_type(session, garage, 90)

    slots = {
        s["start"]: s["status"]
        for s in client.get(
            f"/api/public/{garage.slug}/availability/{day.isoformat()}",
            query_string={"appointment_type_id": str(ninety_min_type.id)},
        ).get_json()["slots"]
    }
    # Every one of these candidate 90-minute windows overlaps the existing
    # 12:00-13:30 booking, not just the one starting at its own 12:00.
    assert slots["11:00"] == "booked"
    assert slots["11:30"] == "booked"
    assert slots["12:00"] == "booked"
    assert slots["12:30"] == "booked"
    assert slots["13:00"] == "booked"
    # 09:00-10:30 and 13:30-15:00 don't overlap it.
    assert slots["09:00"] == "available"
    assert slots["13:30"] == "available"


def test_pending_request_reserves_its_full_duration_not_just_its_start_time(
    client, session, garage
):
    """Item 15's regression: a PENDING 90-minute request at 10:00 must
    reserve 10:00-11:30 in full - a different customer asking about 10:30
    must see it as unavailable too, not just the exact 10:00 start."""
    day = _future_weekday()
    ninety_min_type = _make_type(session, garage, 90, "Full Service")
    thirty_min_type = _make_type(session, garage, 30, "Diagnostic Check")

    session.add(
        BookingRequest(
            garage_id=garage.id,
            status="PENDING",
            customer_first_name="Sam",
            customer_last_name="Lee",
            customer_email="sam.lee@example.com",
            vehicle_registration="PB11 AAA",
            appointment_type_id=ninety_min_type.id,
            requested_duration_minutes=90,
            preferred_date=day,
            preferred_time=datetime.time(10, 0),
        )
    )
    session.commit()

    slots = {
        s["start"]: s["status"]
        for s in client.get(
            f"/api/public/{garage.slug}/availability/{day.isoformat()}",
            query_string={"appointment_type_id": str(thirty_min_type.id)},
        ).get_json()["slots"]
    }
    assert slots["10:00"] == "booked"
    # A 30-minute slot starting at 10:30 would still finish at 11:00, deep
    # inside the pending request's reserved 10:00-11:30 window.
    assert slots["10:30"] == "booked"
    assert slots["11:00"] == "booked"
    # 11:30 is the pending request's own end time - free again from here.
    assert slots["11:30"] == "available"


def test_tenant_isolation(client, session, garage, second_garage):
    day = _future_weekday()
    session.add(
        BookingRequest(
            garage_id=second_garage.id,
            status="PENDING",
            customer_first_name="Other",
            customer_last_name="Garage",
            customer_email="other@example.com",
            vehicle_registration="ZZ99 ZZZ",
            preferred_date=day,
            preferred_time=datetime.time(10, 0),
        )
    )
    session.commit()

    slots = {
        s["start"]: s
        for s in client.get(
            f"/api/public/{garage.slug}/availability/{day.isoformat()}"
        ).get_json()["slots"]
    }
    assert slots["10:00"]["status"] == "available"
