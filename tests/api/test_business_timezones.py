"""Regression coverage for UK business wall-clock booking slots."""

from datetime import UTC, date, datetime, time

from app.garages.timezones import local_slot_as_utc
from app.models.appointments.appointment import Appointment
from app.public_booking.availability import single_day


def test_london_wall_clock_slot_converts_correctly_in_gmt_and_bst(garage):
    garage.timezone = "Europe/London"
    assert local_slot_as_utc(garage, date(2027, 1, 11), time(9)).isoformat() == (
        "2027-01-11T09:00:00+00:00"
    )
    assert local_slot_as_utc(garage, date(2027, 7, 12), time(9)).isoformat() == (
        "2027-07-12T08:00:00+00:00"
    )


def test_london_dst_boundary_keeps_nine_am_as_nine_am_local(garage):
    garage.timezone = "Europe/London"
    # Europe/London switches to BST on this Sunday. A configured 09:00 is
    # still 09:00 to the business, represented by 08:00 UTC.
    assert local_slot_as_utc(garage, date(2027, 3, 28), time(9)).isoformat() == (
        "2027-03-28T08:00:00+00:00"
    )


def test_bst_availability_blocks_the_same_persisted_business_local_slot(
    session, garage, garage_schedule, user, customer, appointment_type
):
    garage.timezone = "Europe/London"
    day = date(2027, 7, 12)
    garage_schedule.min_lead_time_hours = 0
    garage_schedule.max_advance_days = 1000
    start = local_slot_as_utc(garage, day, time(9))
    session.add(
        Appointment(
            garage_id=garage.id,
            employee_id=user.id,
            customer_id=customer.id,
            appointment_type_id=appointment_type.id,
            start_time=start,
            end_time=start.replace(hour=9),
            status="BOOKED",
        )
    )
    session.commit()

    payload = single_day(garage, day, datetime(2027, 7, 1, tzinfo=UTC), appointment_type)
    slots = {slot["start"]: slot["status"] for slot in payload["slots"]}
    assert slots["09:00"] == "booked"
