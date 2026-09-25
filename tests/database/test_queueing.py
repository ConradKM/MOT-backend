"""Database-level guarantees of the walk-in queue tables."""

import datetime

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.queueing.queue_entry import QueueEntry
from app.models.queueing.queue_settings import GarageQueueSettings
from app.models.queueing.reserved_window import WalkInReservedWindow

DAY = datetime.date(2026, 3, 2)


def _entry(garage, ticket, token_hash, **kw):
    return QueueEntry(
        garage_id=garage.id,
        service_date=kw.pop("service_date", DAY),
        ticket_number=ticket,
        sort_key=ticket,
        customer_first_name="Sam",
        customer_phone="+447123456789",
        service_minutes=60,
        public_token_hash=token_hash,
        **kw,
    )


def test_entry_defaults(session, garage):
    entry = _entry(garage, 1, "a" * 64)
    session.add(entry)
    session.commit()
    assert entry.status == "WAITING"
    assert entry.sms_opt_in is False
    assert entry.appointment_id is None


def test_ticket_numbers_are_unique_per_garage_per_day(session, garage, second_garage):
    session.add_all(
        [
            _entry(garage, 1, "a" * 64),
            # Same number, different day or different garage: fine.
            _entry(garage, 1, "b" * 64, service_date=DAY + datetime.timedelta(days=1)),
            _entry(second_garage, 1, "c" * 64),
        ]
    )
    session.commit()
    session.add(_entry(garage, 1, "d" * 64))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_token_hashes_are_unique(session, garage):
    session.add(_entry(garage, 1, "a" * 64))
    session.commit()
    session.add(_entry(garage, 2, "a" * 64))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_one_settings_row_per_garage(session, garage):
    session.add(GarageQueueSettings(garage_id=garage.id))
    session.commit()
    session.add(GarageQueueSettings(garage_id=garage.id))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"weekday": None, "date": None},
        {"weekday": 0, "date": DAY},
        {"weekday": 0, "starts_at": datetime.time(12), "ends_at": datetime.time(9)},
        {"weekday": 0, "reserved_capacity": 0},
    ],
)
def test_reserved_window_check_constraints(session, garage, kwargs):
    values = {
        "starts_at": datetime.time(9),
        "ends_at": datetime.time(12),
        "reserved_capacity": 1,
        **kwargs,
    }
    session.add(WalkInReservedWindow(garage_id=garage.id, **values))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_deleting_a_garage_removes_its_queue_rows(session, garage):
    session.add_all(
        [
            _entry(garage, 1, "a" * 64),
            GarageQueueSettings(garage_id=garage.id),
            WalkInReservedWindow(
                garage_id=garage.id,
                weekday=0,
                starts_at=datetime.time(9),
                ends_at=datetime.time(12),
            ),
        ]
    )
    session.commit()
    session.delete(garage)
    session.commit()
    assert QueueEntry.query.count() == 0
    assert GarageQueueSettings.query.count() == 0
    assert WalkInReservedWindow.query.count() == 0
