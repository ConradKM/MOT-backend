"""Model-level tests for MOTRecord (app/models/mot_record.py)."""

import datetime

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.mot_record import MOTRecord


def test_mot_record_can_be_created(session, garage, vehicle):
    r = MOTRecord(
        garage_id=garage.id,
        vehicle_id=vehicle.id,
        mot_date=datetime.date(2026, 1, 1),
        expiry_date=datetime.date(2027, 1, 1),
        result="PASS",
    )
    session.add(r)
    session.commit()

    assert r.id is not None


def test_mot_record_requires_vehicle_id(session, garage):
    r = MOTRecord(
        garage_id=garage.id,
        mot_date=datetime.date(2026, 1, 1),
        expiry_date=datetime.date(2027, 1, 1),
        result="PASS",
    )
    session.add(r)

    with pytest.raises(IntegrityError):
        session.commit()


def test_mot_record_requires_mot_date_and_expiry_date(session, garage, vehicle):
    r = MOTRecord(garage_id=garage.id, vehicle_id=vehicle.id, result="PASS")
    session.add(r)

    with pytest.raises(IntegrityError):
        session.commit()


def test_mot_record_belongs_to_a_vehicle(mot_record, vehicle):
    assert mot_record.vehicle_id == vehicle.id


def test_mot_record_belongs_to_a_garage(mot_record, garage):
    assert mot_record.garage_id == garage.id


def test_mot_date_and_expiry_date_are_stored_correctly(session, garage, vehicle):
    mot_date = datetime.date(2026, 3, 15)
    expiry_date = datetime.date(2027, 3, 15)

    r = MOTRecord(
        garage_id=garage.id,
        vehicle_id=vehicle.id,
        mot_date=mot_date,
        expiry_date=expiry_date,
        result="FAIL",
        notes="Brake pads worn.",
    )
    session.add(r)
    session.commit()
    session.refresh(r)

    assert r.mot_date == mot_date
    assert r.expiry_date == expiry_date
    assert r.result == "FAIL"
    assert r.notes == "Brake pads worn."


def test_mot_history_can_contain_multiple_records_for_the_same_vehicle(session, garage, vehicle):
    r1 = MOTRecord(
        garage_id=garage.id,
        vehicle_id=vehicle.id,
        mot_date=datetime.date(2024, 1, 1),
        expiry_date=datetime.date(2025, 1, 1),
        result="PASS",
    )
    r2 = MOTRecord(
        garage_id=garage.id,
        vehicle_id=vehicle.id,
        mot_date=datetime.date(2025, 1, 1),
        expiry_date=datetime.date(2026, 1, 1),
        result="PASS",
    )
    session.add_all([r1, r2])
    session.commit()

    records = MOTRecord.query.filter_by(vehicle_id=vehicle.id).all()
    assert len(records) == 2


def test_mot_record_vehicle_relationship_works(session, mot_record, vehicle):
    session.refresh(mot_record)
    assert mot_record.vehicle.id == vehicle.id


def test_mot_record_garage_relationship_works(session, mot_record, garage):
    session.refresh(mot_record)
    assert mot_record.garage.id == garage.id


def test_mot_record_timestamps_are_populated(mot_record):
    assert mot_record.created_at is not None
    assert mot_record.updated_at is not None
