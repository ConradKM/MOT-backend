"""API tests for GET /api/garage/capacity/summary (staff dashboard).

Capacity is measured in minutes of scheduling time (open minutes * number of
parallel resources), not a count of appointment rows against a count of
theoretical slots - see app/garages/capacity.py for why a raw count is
misleading once appointment durations vary.
"""

import datetime

from app.garages.capacity import _level
from app.models.garage_schedule import GarageScheduleException
from app.public_booking.availability import (
    day_open_minutes,
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


def _expected_today_capacity_minutes(garage):
    settings = resolve_settings(garage)
    hours = resolve_opening_hours(garage)
    exc = resolve_exceptions(garage, TODAY, TODAY)
    per_slot = slot_capacity(garage, settings)
    return day_open_minutes(TODAY, hours, exc) * per_slot


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

    assert set(body["today"]) == {"date", "booked_minutes", "capacity_minutes", "level"}
    assert set(body["week"]) == {
        "start", "end", "booked_minutes", "capacity_minutes", "level",
    }
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
    assert body["today"]["capacity_minutes"] == _expected_today_capacity_minutes(garage)
    # The week spans today, so weekly capacity is at least today's.
    assert body["week"]["capacity_minutes"] >= body["today"]["capacity_minutes"]


def test_booked_counts_minutes_of_non_cancelled_appointments_today(
    authenticated_client, session, garage, garage_schedule, make_appointment
):
    make_appointment(_at(9, 0), minutes=60)
    make_appointment(_at(13, 0), minutes=45)
    make_appointment(_at(15, 0), minutes=90, status="CANCELLED")

    body = authenticated_client.get("/api/garage/capacity/summary").get_json()
    assert body["today"]["booked_minutes"] == 105  # 60 + 45, cancelled excluded
    assert body["week"]["booked_minutes"] >= 105


def test_closure_zeroes_the_day(
    authenticated_client, session, garage, garage_schedule
):
    session.add(
        GarageScheduleException(garage_id=garage.id, date=TODAY, is_closed=True)
    )
    session.commit()

    body = authenticated_client.get("/api/garage/capacity/summary").get_json()
    assert body["today"]["capacity_minutes"] == 0


def test_capacity_per_slot_override_and_level(
    authenticated_client, session, garage, garage_schedule, make_appointment
):
    # Tiny single-resource opening window on every weekday so the numbers
    # stay easy to reason about.
    from app.models.garage_schedule import GarageOpeningHours

    GarageOpeningHours.query.filter_by(garage_id=garage.id).delete()
    for wd in range(7):
        session.add(
            GarageOpeningHours(
                garage_id=garage.id,
                weekday=wd,
                opens_at=datetime.time(9, 0),
                closes_at=datetime.time(11, 0),
                is_closed=False,
            )
        )
    garage_schedule.capacity_per_slot = 1
    session.commit()

    # 09:00-11:00, one resource -> 120 minutes of capacity.
    body = authenticated_client.get("/api/garage/capacity/summary").get_json()
    assert body["today"]["capacity_minutes"] == 120
    assert body["today"]["level"] == "green"

    make_appointment(_at(9, 0), minutes=60)  # 60 / 120 = 50% -> green
    body = authenticated_client.get("/api/garage/capacity/summary").get_json()
    assert body["today"]["booked_minutes"] == 60
    assert body["today"]["level"] == "green"

    make_appointment(_at(10, 0), minutes=30)  # 90 / 120 = 75% -> amber
    body = authenticated_client.get("/api/garage/capacity/summary").get_json()
    assert body["today"]["booked_minutes"] == 90
    assert body["today"]["level"] == "amber"

    make_appointment(_at(10, 30), minutes=30)  # 120 / 120 = 100% -> red
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

    assert mine["today"]["booked_minutes"] == 60
    assert theirs["today"]["booked_minutes"] == 0


def test_requires_auth(client):
    assert client.get("/api/garage/capacity/summary").status_code == 401
