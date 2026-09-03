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
