"""Walk-in reserved windows: CRUD, and how they withhold capacity from the
public booking calendar (app/public_booking/availability.py) without
restricting staff scheduling."""

import datetime

import pytest

from app.models.queueing.reserved_window import WalkInReservedWindow
from app.public_booking.availability import single_day, validate_slot

UTC = datetime.UTC
# A Monday well clear of "now" so minimum-notice rules never interfere.
_real_today = datetime.datetime.now(UTC).date()
MONDAY = _real_today + datetime.timedelta(days=14 - _real_today.weekday())
NEXT_MONDAY = MONDAY + datetime.timedelta(days=7)
NOW = datetime.datetime.combine(_real_today, datetime.time(8), tzinfo=UTC)


def _t(hour, minute=0):
    return datetime.time(hour, minute)


def _slot(day_payload, start):
    return next(s for s in day_payload["slots"] if s["start"] == start)


@pytest.fixture()
def window(session, garage):
    def _make(**kwargs):
        kwargs.setdefault("reserved_capacity", 1)
        w = WalkInReservedWindow(garage_id=garage.id, **kwargs)
        session.add(w)
        session.commit()
        return w

    return _make


# --------------------------------------------------------------------------
# Availability integration
# --------------------------------------------------------------------------


def test_recurring_window_withholds_the_only_bay_from_public_booking(
    garage, garage_schedule, window
):
    garage_schedule.capacity_per_slot = 1
    window(weekday=0, starts_at=_t(9), ends_at=_t(12))

    day = single_day(garage, MONDAY, NOW)
    assert _slot(day, "09:00")["status"] == "booked"
    # 11:05-12:05 still overlaps the window: a booking needs its bay throughout.
    assert _slot(day, "11:05")["status"] == "booked"
    assert _slot(day, "12:00")["status"] == "available"
    assert validate_slot(garage, MONDAY, _t(10), NOW) == "full"
    assert validate_slot(garage, MONDAY, _t(13), NOW) is None


def test_window_reserves_some_bays_and_bookings_fill_the_rest(
    garage, garage_schedule, window, make_appointment
):
    garage_schedule.capacity_per_slot = 2
    window(weekday=0, starts_at=_t(9), ends_at=_t(12))
    day = single_day(garage, MONDAY, NOW)
    assert _slot(day, "10:00")["remaining"] == 1

    make_appointment(datetime.datetime.combine(MONDAY, _t(10), tzinfo=UTC))
    day = single_day(garage, MONDAY, NOW)
    assert _slot(day, "10:00")["status"] == "booked"
    assert _slot(day, "13:00")["remaining"] == 2


def test_one_off_window_applies_to_its_date_only(garage, garage_schedule, window):
    garage_schedule.capacity_per_slot = 1
    window(date=NEXT_MONDAY, starts_at=_t(9), ends_at=_t(17))
    assert validate_slot(garage, NEXT_MONDAY, _t(10), NOW) == "full"
    assert validate_slot(garage, MONDAY, _t(10), NOW) is None


def test_overlapping_windows_take_the_largest_reservation_not_the_sum(
    garage, garage_schedule, window
):
    garage_schedule.capacity_per_slot = 3
    window(weekday=0, starts_at=_t(9), ends_at=_t(12), reserved_capacity=1)
    window(date=MONDAY, starts_at=_t(10), ends_at=_t(11), reserved_capacity=2)
    assert _slot(single_day(garage, MONDAY, NOW), "10:00")["remaining"] == 1


def test_other_garages_windows_do_not_apply(garage, garage_schedule, second_garage, session):
    garage_schedule.capacity_per_slot = 1
    session.add(
        WalkInReservedWindow(
            garage_id=second_garage.id,
            weekday=0,
            starts_at=_t(9),
            ends_at=_t(17),
            reserved_capacity=5,
        )
    )
    session.commit()
    assert validate_slot(garage, MONDAY, _t(10), NOW) is None


def test_staff_can_still_book_inside_a_walk_in_window(
    authenticated_client, garage, garage_schedule, window, customer, appointment_type, user
):
    garage_schedule.capacity_per_slot = 1
    window(weekday=0, starts_at=_t(9), ends_at=_t(12))
    start = datetime.datetime.combine(MONDAY, _t(10), tzinfo=UTC)
    resp = authenticated_client.post(
        "/api/appointments/",
        json={
            "employee_id": str(user.id),
            "customer_id": str(customer.id),
            "appointment_type_id": str(appointment_type.id),
            "start_time": start.isoformat(),
            "end_time": (start + datetime.timedelta(hours=1)).isoformat(),
        },
    )
    assert resp.status_code == 201, resp.get_json()


# --------------------------------------------------------------------------
# CRUD
# --------------------------------------------------------------------------


def test_create_list_and_delete_windows(authenticated_client, garage):
    weekly = authenticated_client.post(
        "/api/queue/reserved-windows",
        json={"weekday": 2, "starts_at": "08:00", "ends_at": "10:00", "reserved_capacity": 2},
    )
    assert weekly.status_code == 201
    one_off = authenticated_client.post(
        "/api/queue/reserved-windows",
        json={"date": NEXT_MONDAY.isoformat(), "starts_at": "13:00", "ends_at": "15:00"},
    )
    assert one_off.status_code == 201
    assert one_off.get_json()["reserved_capacity"] == 1

    listed = authenticated_client.get("/api/queue/reserved-windows").get_json()
    # Recurring first, then dated.
    assert [w["weekday"] for w in listed] == [2, None]

    resp = authenticated_client.delete(f"/api/queue/reserved-windows/{weekly.get_json()['id']}")
    assert resp.status_code == 204
    assert len(authenticated_client.get("/api/queue/reserved-windows").get_json()) == 1


@pytest.mark.parametrize(
    "body",
    [
        {"starts_at": "08:00", "ends_at": "10:00"},  # neither weekday nor date
        {"weekday": 1, "date": "2030-01-07", "starts_at": "08:00", "ends_at": "10:00"},
        {"weekday": 1, "starts_at": "10:00", "ends_at": "10:00"},
        {"weekday": 7, "starts_at": "08:00", "ends_at": "10:00"},
        {"weekday": 1, "starts_at": "08:00", "ends_at": "10:00", "reserved_capacity": 0},
    ],
)
def test_invalid_windows_are_rejected(authenticated_client, garage, body):
    assert authenticated_client.post("/api/queue/reserved-windows", json=body).status_code == 422


def test_cannot_delete_another_garages_window(
    second_authenticated_client, garage, window, second_garage
):
    w = window(weekday=0, starts_at=_t(9), ends_at=_t(12))
    assert (
        second_authenticated_client.delete(f"/api/queue/reserved-windows/{w.id}").status_code == 404
    )
    assert WalkInReservedWindow.query.count() == 1
