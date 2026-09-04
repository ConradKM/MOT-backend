"""API tests for GET /api/garage/capacity/summary (staff dashboard)."""

import datetime

from app.garages.capacity import _level
from app.models.garage_schedule import GarageScheduleException
from app.public_booking.availability import (
    day_slot_count,
    resolve_exceptions,
    resolve_opening_hours,
    resolve_settings,
    slot_capacity,
)

UTC = datetime.UTC
TODAY = datetime.date.today()


def _at(hour, minute=0):
    return datetime.datetime.combine(
        TODAY, datetime.time(hour, minute), tzinfo=UTC
    )


def _expected_today_capacity(garage):
    settings = resolve_settings(garage)
    hours = resolve_opening_hours(garage)
    exc = resolve_exceptions(garage, TODAY, TODAY)
    per_slot = slot_capacity(garage, settings)
    return day_slot_count(TODAY, settings, hours, exc) * per_slot


def test_level_thresholds():
    assert _level(0, 10) == "green"
    assert _level(6, 10) == "green"
    assert _level(7, 10) == "amber"
    assert _level(9, 10) == "amber"
    assert _level(10, 10) == "red"
    assert _level(12, 10) == "red"
    assert _level(0, 0) == "green"
    assert _level(1, 0) == "red"


def test_summary_shape(authenticated_client, garage, garage_schedule):
    body = authenticated_client.get("/api/garage/capacity/summary").get_json()

    assert set(body["today"]) == {"date", "booked", "capacity", "level"}
    assert set(body["week"]) == {"start", "end", "booked", "capacity", "level"}
    assert body["today"]["date"] == TODAY.isoformat()
    assert body["today"]["level"] in {"green", "amber", "red"}
    # Monday .. Sunday containing today
    ws = datetime.date.fromisoformat(body["week"]["start"])
    we = datetime.date.fromisoformat(body["week"]["end"])
    assert ws.weekday() == 0 and (we - ws).days == 6 and ws <= TODAY <= we


def test_capacity_matches_the_schedule(
    authenticated_client, garage, garage_schedule
):
    body = authenticated_client.get("/api/garage/capacity/summary").get_json()
    assert body["today"]["capacity"] == _expected_today_capacity(garage)
    # The week spans today, so weekly capacity is at least today's.
    assert body["week"]["capacity"] >= body["today"]["capacity"]


def test_booked_counts_non_cancelled_appointments_today(
    authenticated_client, session, garage, garage_schedule, make_appointment
):
    make_appointment(_at(9, 0), minutes=60)
    make_appointment(_at(10, 0), minutes=60)
    make_appointment(_at(11, 0), minutes=60, status="CANCELLED")

    body = authenticated_client.get("/api/garage/capacity/summary").get_json()
    assert body["today"]["booked"] == 2
    assert body["week"]["booked"] >= 2


def test_closure_zeroes_the_day(
    authenticated_client, session, garage, garage_schedule
):
    session.add(
        GarageScheduleException(garage_id=garage.id, date=TODAY, is_closed=True)
    )
    session.commit()

    body = authenticated_client.get("/api/garage/capacity/summary").get_json()
    assert body["today"]["capacity"] == 0


def test_capacity_per_slot_override_and_level(
    authenticated_client, session, garage, garage_schedule, make_appointment
):
    # Tiny opening window on every weekday so the numbers are small.
    from app.models.garage_schedule import GarageOpeningHours

    GarageOpeningHours.query.filter_by(garage_id=garage.id).delete()
    for wd in range(7):
        session.add(
            GarageOpeningHours(
                garage_id=garage.id,
                weekday=wd,
                opens_at=datetime.time(9, 0),
                closes_at=datetime.time(11, 30),
                is_closed=False,
            )
        )
    garage_schedule.capacity_per_slot = 1
    session.commit()

    # 09:00-11:30, 30-min slots, 60-min duration -> starts 09:00 09:30 10:00 10:30 = 4
    body = authenticated_client.get("/api/garage/capacity/summary").get_json()
    assert body["today"]["capacity"] == 4
    assert body["today"]["level"] == "green"

    make_appointment(_at(9, 0))
    make_appointment(_at(9, 30))
    make_appointment(_at(10, 0))  # 3 / 4 = 75% -> amber
    body = authenticated_client.get("/api/garage/capacity/summary").get_json()
    assert body["today"]["booked"] == 3
    assert body["today"]["level"] == "amber"

    make_appointment(_at(10, 30))  # 4 / 4 -> red
    body = authenticated_client.get("/api/garage/capacity/summary").get_json()
    assert body["today"]["level"] == "red"


def test_summary_is_tenant_scoped(
    authenticated_client,
    second_authenticated_client,
    session,
    garage,
    garage_schedule,
    make_appointment,
):
    make_appointment(_at(9, 0), minutes=60)

    mine = authenticated_client.get("/api/garage/capacity/summary").get_json()
    theirs = second_authenticated_client.get(
        "/api/garage/capacity/summary"
    ).get_json()

    assert mine["today"]["booked"] == 1
    assert theirs["today"]["booked"] == 0


def test_requires_auth(client):
    assert client.get("/api/garage/capacity/summary").status_code == 401
