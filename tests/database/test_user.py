"""Model-level tests for User (app/models/user.py)."""

import pytest
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash

from app.models.user import User


def test_user_can_be_created(session, garage):
    u = User(
        garage_id=garage.id,
        email="new-user@example.com",
        password_hash=generate_password_hash("password123"),
        role="OWNER",
    )
    session.add(u)
    session.commit()

    assert u.id is not None


def test_user_belongs_to_a_garage(user, garage):
    assert user.garage_id == garage.id


def test_user_requires_garage_id(session):
    u = User(email="no-garage@example.com", password_hash="x")
    session.add(u)

    with pytest.raises(IntegrityError):
        session.commit()


def test_user_requires_email(session, garage):
    u = User(garage_id=garage.id, password_hash="x")
    session.add(u)

    with pytest.raises(IntegrityError):
        session.commit()


def test_email_uniqueness_is_enforced(session, garage, user):
    duplicate = User(
        garage_id=garage.id,
        email=user.email,
        password_hash="x",
    )
    session.add(duplicate)

    with pytest.raises(IntegrityError):
        session.commit()


def test_password_is_stored_as_a_hash_never_plaintext(session, garage):
    plaintext = "supersecret123"
    u = User(
        garage_id=garage.id,
        email="hashed@example.com",
        password_hash=generate_password_hash(plaintext),
    )
    session.add(u)
    session.commit()

    assert u.password_hash != plaintext
    assert plaintext not in u.password_hash
    assert check_password_hash(u.password_hash, plaintext)


def test_user_garage_relationship_works(session, user, garage):
    session.refresh(user)
    assert user.garage.id == garage.id
    assert user.garage.name == garage.name


def test_different_users_can_belong_to_different_garages(session, garage, second_garage):
    u1 = User(garage_id=garage.id, email="u1@example.com", password_hash="x")
    u2 = User(garage_id=second_garage.id, email="u2@example.com", password_hash="x")
    session.add_all([u1, u2])
    session.commit()

    assert u1.garage_id != u2.garage_id
    assert u1.garage.id == garage.id
    assert u2.garage.id == second_garage.id


def test_user_timestamps_are_populated(user):
    assert user.created_at is not None
    assert user.updated_at is not None


def test_user_default_role_is_owner(session, garage):
    u = User(garage_id=garage.id, email="default-role@example.com", password_hash="x")
    session.add(u)
    session.commit()

    assert u.role == "OWNER"


def test_user_has_role_helper(user):
    assert user.has_role("OWNER")
    assert not user.has_role("STAFF")
    assert user.has_role("STAFF", "OWNER")
