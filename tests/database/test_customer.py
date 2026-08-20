"""Model-level tests for Customer (app/models/customer.py)."""

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.customer import Customer
from app.models.vehicle import Vehicle


def test_customer_can_be_created(session, garage):
    c = Customer(garage_id=garage.id, first_name="Jane", last_name="Doe")
    session.add(c)
    session.commit()

    assert c.id is not None


def test_customer_requires_garage_id(session):
    c = Customer(first_name="Jane", last_name="Doe")
    session.add(c)

    with pytest.raises(IntegrityError):
        session.commit()


def test_customer_requires_first_and_last_name(session, garage):
    c = Customer(garage_id=garage.id)
    session.add(c)

    with pytest.raises(IntegrityError):
        session.commit()


def test_customer_belongs_to_a_garage(customer, garage):
    assert customer.garage_id == garage.id


def test_customer_fields_are_stored_correctly(session, garage):
    c = Customer(
        garage_id=garage.id,
        first_name="Jane",
        last_name="Doe",
        email="jane@example.com",
        phone="+44 7700 900123",
    )
    session.add(c)
    session.commit()
    session.refresh(c)

    assert c.first_name == "Jane"
    assert c.last_name == "Doe"
    assert c.email == "jane@example.com"
    assert c.phone == "+44 7700 900123"


def test_customer_email_and_phone_are_optional(session, garage):
    c = Customer(garage_id=garage.id, first_name="Jane", last_name="Doe")
    session.add(c)
    session.commit()

    assert c.email is None
    assert c.phone is None


def test_customer_garage_relationship_works(session, customer, garage):
    session.refresh(customer)
    assert customer.garage.id == garage.id


def test_customer_vehicles_relationship_works(session, customer):
    v1 = Vehicle(garage_id=customer.garage_id, customer_id=customer.id, registration_number="AA11AAA")
    v2 = Vehicle(garage_id=customer.garage_id, customer_id=customer.id, registration_number="BB22BBB")
    session.add_all([v1, v2])
    session.commit()

    session.refresh(customer)
    assert {v.id for v in customer.vehicles} == {v1.id, v2.id}


def test_customer_timestamps_are_populated(customer):
    assert customer.created_at is not None
    assert customer.updated_at is not None


def test_deleting_customer_cascades_to_vehicles(session, customer, vehicle):
    vehicle_id = vehicle.id

    session.delete(customer)
    session.commit()

    assert session.get(Vehicle, vehicle_id) is None
