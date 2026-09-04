"""Same-day booking: availability + submit re-check (ConradKM/MOT-backend#28).

`validate_slot` is unit-tested with a frozen ``now``; the submit endpoint is
tested with ``app.public_booking.routes.datetime`` frozen to the same instant.
"""

import datetime

from app.models.booking_request import BookingRequest
from app.models.garage_schedule import GarageScheduleException
from app.public_booking.availability import single_day, validate_slot

UTC = datetime.UTC
# 2026-09-07 is a Monday; the garage's default hours are Mon-Fri 09:00-17:00.
NOW = datetime.datetime(2026, 9, 7, 10, 15, tzinfo=UTC)
TODAY = NOW.date()
SATURDAY = datetime.date(2026, 9, 12)
NEXT_MONDAY = datetime.date(2026, 9, 14)


def _t(hour, minute=0):
    return datetime.time(hour, minute)


# --------------------------------------------------------------------------
# validate_slot
# --------------------------------------------------------------------------


def test_valid_future_slot_today_is_bookable(garage, garage_schedule):
    # 14:00 today: open, in the future, past the 2h notice, has capacity.
    assert validate_slot(garage, TODAY, _t(14, 0), NOW) is None


def test_past_time_today_is_rejected(garage, garage_schedule):
    assert validate_slot(garage, TODAY, _t(9, 30), NOW) == "past"


def test_slot_inside_minimum_notice_is_rejected(garage, garage_schedule):
    # now 10:15 + 2h notice = 12:15; 11:00 is future but too soon.
    assert validate_slot(garage, TODAY, _t(11, 0), NOW) == "too_soon"


def test_yesterday_is_rejected(garage, garage_schedule):
    assert validate_slot(garage, TODAY - datetime.timedelta(days=1), _t(14, 0), NOW) == "past"


def test_closed_weekend_is_rejected(garage, garage_schedule):
    assert validate_slot(garage, SATURDAY, _t(10, 0), NOW) == "closed"


def test_closure_override_is_rejected(session, garage, garage_schedule):
    session.add(
        GarageScheduleException(garage_id=garage.id, date=NEXT_MONDAY, is_closed=True)
    )
    session.commit()
    assert validate_slot(garage, NEXT_MONDAY, _t(10, 0), NOW) == "closed"


def test_outside_opening_hours_is_rejected(garage, garage_schedule):
    assert validate_slot(garage, NEXT_MONDAY, _t(8, 0), NOW) == "outside_hours"
    # 16:30 + 60-min duration spills past 17:00.
    assert validate_slot(garage, NEXT_MONDAY, _t(16, 30), NOW) == "outside_hours"


def test_beyond_the_booking_window_is_rejected(garage, garage_schedule):
    far = TODAY + datetime.timedelta(days=90)
    assert validate_slot(garage, far, _t(10, 0), NOW) == "out_of_window"


def test_full_slot_is_rejected(garage, garage_schedule, make_appointment):
    # Default capacity with one employee is 1; fill 14:00 next Monday.
    make_appointment(
        datetime.datetime.combine(NEXT_MONDAY, _t(14, 0), tzinfo=UTC), minutes=60
    )
    assert validate_slot(garage, NEXT_MONDAY, _t(14, 0), NOW) == "full"


def test_future_dates_are_unaffected(garage, garage_schedule):
    assert validate_slot(garage, NEXT_MONDAY, _t(10, 0), NOW) is None


def test_single_day_today_lists_only_valid_future_slots(garage, garage_schedule):
    body = single_day(garage, TODAY, NOW)
    assert body["is_open"] is True
    starts = [s["start"] for s in body["slots"]]
    # now 10:15 + 2h notice -> first bookable slot start is 12:30.
    assert starts and starts[0] == "12:30"
    assert "09:00" not in starts and "11:00" not in starts


# --------------------------------------------------------------------------
# POST /api/public/<slug>/booking-requests  (submit re-check)
# --------------------------------------------------------------------------


def _freeze_now(monkeypatch):
    class _Frozen(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr("app.public_booking.routes.datetime", _Frozen)


def _payload(**overrides):
    data = {
        "customer_first_name": "Sam",
        "customer_last_name": "Day",
        "customer_email": "sam.day@example.com",
        "vehicle_registration": "SD11 DAY",
        "preferred_date": TODAY.isoformat(),
        "preferred_time": "14:00:00",
    }
    data.update(overrides)
    return data


def test_submit_valid_same_day_booking_succeeds(
    client, session, garage, garage_schedule, monkeypatch
):
    _freeze_now(monkeypatch)
    resp = client.post(
        f"/api/public/{garage.slug}/booking-requests", json=_payload()
    )
    assert resp.status_code == 201
    assert BookingRequest.query.count() == 1


def test_submit_past_time_today_is_rejected(
    client, session, garage, garage_schedule, monkeypatch
):
    _freeze_now(monkeypatch)
    resp = client.post(
        f"/api/public/{garage.slug}/booking-requests",
        json=_payload(preferred_time="09:30:00"),
    )
    assert resp.status_code == 409
    assert "past" in resp.get_json()["message"].lower()
    assert BookingRequest.query.count() == 0


def test_submit_inside_notice_is_rejected(
    client, session, garage, garage_schedule, monkeypatch
):
    _freeze_now(monkeypatch)
    resp = client.post(
        f"/api/public/{garage.slug}/booking-requests",
        json=_payload(preferred_time="11:00:00"),
    )
    assert resp.status_code == 409
    assert BookingRequest.query.count() == 0


def test_submit_closed_day_is_rejected(
    client, session, garage, garage_schedule, monkeypatch
):
    _freeze_now(monkeypatch)
    resp = client.post(
        f"/api/public/{garage.slug}/booking-requests",
        json=_payload(preferred_date=SATURDAY.isoformat(), preferred_time="10:00:00"),
    )
    assert resp.status_code == 409
    assert BookingRequest.query.count() == 0


def test_submit_future_date_still_works(
    client, session, garage, garage_schedule, monkeypatch
):
    _freeze_now(monkeypatch)
    resp = client.post(
        f"/api/public/{garage.slug}/booking-requests",
        json=_payload(preferred_date=NEXT_MONDAY.isoformat(), preferred_time="10:00:00"),
    )
    assert resp.status_code == 201


def test_submit_date_only_still_skips_the_slot_check(
    client, session, garage, garage_schedule, monkeypatch
):
    _freeze_now(monkeypatch)
    resp = client.post(
        f"/api/public/{garage.slug}/booking-requests",
        json=_payload(preferred_time=None),
    )
    assert resp.status_code == 201
