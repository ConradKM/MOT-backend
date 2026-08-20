"""Model-level tests for Vehicle (app/models/vehicle.py)."""

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.mot_record import MOTRecord
from app.models.vehicle import Vehicle


def test_vehicle_can_be_created(session, garage, customer):
    v = Vehicle(garage_id=garage.id, customer_id=customer.id, registration_number="AB12CDE")
    session.add(v)
    session.commit()

    assert v.id is not None


def test_vehicle_requires_garage_id(session, customer):
    v = Vehicle(customer_id=customer.id, registration_number="AB12CDE")
    session.add(v)

    with pytest.raises(IntegrityError):
        session.commit()


def test_vehicle_requires_customer_id(session, garage):
    v = Vehicle(garage_id=garage.id, registration_number="AB12CDE")
    session.add(v)

    with pytest.raises(IntegrityError):
        session.commit()


def test_vehicle_requires_registration_number(session, garage, customer):
    v = Vehicle(garage_id=garage.id, customer_id=customer.id)
    session.add(v)

    with pytest.raises(IntegrityError):
        session.commit()


def test_vehicle_belongs_to_a_garage(vehicle, garage):
    assert vehicle.garage_id == garage.id


def test_vehicle_belongs_to_a_customer(vehicle, customer):
    assert vehicle.customer_id == customer.id


def test_vehicle_fields_are_persisted_correctly(session, garage, customer):
    v = Vehicle(
        garage_id=garage.id,
        customer_id=customer.id,
        registration_number="AB12CDE",
        make="Ford",
        model="Focus",
        year=2020,
        current_mileage=15000,
    )
    session.add(v)
    session.commit()
    session.refresh(v)

    assert v.make == "Ford"
    assert v.model == "Focus"
    assert v.year == 2020
    assert v.current_mileage == 15000


def test_vehicle_registration_number_is_normalized(session, garage, customer):
    v = Vehicle(garage_id=garage.id, customer_id=customer.id, registration_number=" ab12 cde ")
    session.add(v)
    session.commit()

    assert v.registration_number == "AB12CDE"


def test_vehicle_registration_number_unique_per_garage(session, garage, customer):
    v1 = Vehicle(garage_id=garage.id, customer_id=customer.id, registration_number="AB12CDE")
    session.add(v1)
    session.commit()

    v2 = Vehicle(garage_id=garage.id, customer_id=customer.id, registration_number="AB12CDE")
    session.add(v2)

    with pytest.raises(IntegrityError):
        session.commit()


def test_same_registration_number_allowed_in_different_garages(
    session, garage, customer, second_garage, second_customer
):
    v1 = Vehicle(garage_id=garage.id, customer_id=customer.id, registration_number="SAME123")
    v2 = Vehicle(
        garage_id=second_garage.id, customer_id=second_customer.id, registration_number="SAME123"
    )
    session.add_all([v1, v2])
    session.commit()

    assert v1.id is not None
    assert v2.id is not None


def test_vehicle_customer_relationship_works(session, vehicle, customer):
    session.refresh(vehicle)
    assert vehicle.customer.id == customer.id


def test_vehicle_garage_relationship_works(session, vehicle, garage):
    session.refresh(vehicle)
    assert vehicle.garage.id == garage.id


def test_vehicle_mot_records_relationship_works(session, vehicle, garage):
    r1 = MOTRecord(
        garage_id=garage.id,
        vehicle_id=vehicle.id,
        mot_date="2025-01-01",
        expiry_date="2026-01-01",
        result="PASS",
    )
    r2 = MOTRecord(
        garage_id=garage.id,
        vehicle_id=vehicle.id,
        mot_date="2026-01-01",
        expiry_date="2027-01-01",
        result="PASS",
    )
    session.add_all([r1, r2])
    session.commit()

    session.refresh(vehicle)
    assert {r.id for r in vehicle.mot_records} == {r1.id, r2.id}


def test_deleting_vehicle_cascades_to_mot_records(session, vehicle, mot_record):
    record_id = mot_record.id

    session.delete(vehicle)
    session.commit()

    assert session.get(MOTRecord, record_id) is None
