"""Model-level tests for Garage (app/models/garage.py)."""

import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.customer import Customer
from app.models.employee import Employee
from app.models.garage import Garage
from app.models.mot_record import MOTRecord
from app.models.vehicle import Vehicle


def test_garage_can_be_created(session):
    g = Garage(
        name="Acme Motors",
        slug="acme-motors",
        email="hi@acme.example",
        phone="0123",
        address="1 Road",
    )
    session.add(g)
    session.commit()

    assert g.id is not None


def test_garage_requires_name(session):
    g = Garage(slug="no-name")
    session.add(g)

    with pytest.raises(IntegrityError):
        session.commit()


def test_garage_requires_slug(session):
    g = Garage(name="Slugless Motors")
    session.add(g)

    with pytest.raises(IntegrityError):
        session.commit()


def test_garage_receives_an_id(garage):
    assert isinstance(garage.id, uuid.UUID)


def test_garage_timestamps_are_populated(garage):
    assert garage.created_at is not None
    assert garage.updated_at is not None


def test_garage_updated_at_changes_on_update(session, garage):
    original_updated_at = garage.updated_at

    garage.name = "Renamed Garage"
    session.commit()
    session.refresh(garage)

    assert garage.updated_at >= original_updated_at


def test_garage_can_have_employees(session, garage):
    e1 = Employee(garage_id=garage.id, email="a@example.com", password_hash="x")
    e2 = Employee(garage_id=garage.id, email="b@example.com", password_hash="x")
    session.add_all([e1, e2])
    session.commit()

    session.refresh(garage)
    assert {e.id for e in garage.employees} == {e1.id, e2.id}


def test_garage_can_have_customers(session, garage):
    c = Customer(garage_id=garage.id, first_name="Jane", last_name="Doe")
    session.add(c)
    session.commit()

    session.refresh(garage)
    assert c in garage.customers


def test_garage_can_have_vehicles_and_mot_records(session, garage, customer):
    vehicle = Vehicle(
        garage_id=garage.id,
        customer_id=customer.id,
        registration_number="AB12CDE",
    )
    session.add(vehicle)
    session.commit()

    record = MOTRecord(
        garage_id=garage.id,
        vehicle_id=vehicle.id,
        mot_date="2026-01-01",
        expiry_date="2027-01-01",
        result="PASS",
    )
    session.add(record)
    session.commit()

    session.refresh(garage)
    assert vehicle in garage.vehicles
    assert record in garage.mot_records


def test_deleting_garage_cascades_to_children(session, garage, customer, vehicle, mot_record):
    garage_id = garage.id

    session.delete(garage)
    session.commit()

    assert session.get(Garage, garage_id) is None
    assert session.get(Customer, customer.id) is None
    assert session.get(Vehicle, vehicle.id) is None
    assert session.get(MOTRecord, mot_record.id) is None
