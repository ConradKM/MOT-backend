"""Model-level tests for Employee (app/models/employee.py)."""

import pytest
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash

from app.models.employee import Employee


def test_employee_can_be_created(session, garage):
    e = Employee(
        garage_id=garage.id,
        email="new-employee@example.com",
        password_hash=generate_password_hash("password123"),
        role="OWNER",
    )
    session.add(e)
    session.commit()

    assert e.id is not None


def test_employee_belongs_to_a_garage(user, garage):
    assert user.garage_id == garage.id


def test_employee_requires_garage_id(session):
    e = Employee(email="no-garage@example.com", password_hash="x")
    session.add(e)

    with pytest.raises(IntegrityError):
        session.commit()


def test_employee_requires_email(session, garage):
    e = Employee(garage_id=garage.id, password_hash="x")
    session.add(e)

    with pytest.raises(IntegrityError):
        session.commit()


def test_email_uniqueness_is_enforced(session, garage, user):
    duplicate = Employee(
        garage_id=garage.id,
        email=user.email,
        password_hash="x",
    )
    session.add(duplicate)

    with pytest.raises(IntegrityError):
        session.commit()


def test_password_is_stored_as_a_hash_never_plaintext(session, garage):
    plaintext = "supersecret123"
    e = Employee(
        garage_id=garage.id,
        email="hashed@example.com",
        password_hash=generate_password_hash(plaintext),
    )
    session.add(e)
    session.commit()

    assert e.password_hash != plaintext
    assert plaintext not in e.password_hash
    assert check_password_hash(e.password_hash, plaintext)


def test_employee_garage_relationship_works(session, user, garage):
    session.refresh(user)
    assert user.garage.id == garage.id
    assert user.garage.name == garage.name


def test_different_employees_can_belong_to_different_garages(session, garage, second_garage):
    e1 = Employee(garage_id=garage.id, email="e1@example.com", password_hash="x")
    e2 = Employee(garage_id=second_garage.id, email="e2@example.com", password_hash="x")
    session.add_all([e1, e2])
    session.commit()

    assert e1.garage_id != e2.garage_id
    assert e1.garage.id == garage.id
    assert e2.garage.id == second_garage.id


def test_employee_timestamps_are_populated(user):
    assert user.created_at is not None
    assert user.updated_at is not None


def test_employee_default_role_is_owner(session, garage):
    e = Employee(garage_id=garage.id, email="default-role@example.com", password_hash="x")
    session.add(e)
    session.commit()

    assert e.role == "OWNER"


def test_employee_has_role_helper(user):
    assert user.has_role("OWNER")
    assert not user.has_role("STAFF")
    assert user.has_role("STAFF", "OWNER")
