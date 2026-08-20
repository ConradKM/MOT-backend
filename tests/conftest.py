"""Shared pytest fixtures and test-run reporting for the MOT Garage API suite.

Tests run against a dedicated Postgres database (TestConfig.SQLALCHEMY_DATABASE_URI,
default `mot_garage_test`), created automatically if it doesn't exist. The
developer's DATABASE_URL is never read by the test suite. Tables are created once
per session and truncated after every test, so no manual cleanup is required.
"""

import datetime
import os
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from flask_jwt_extended import create_access_token, create_refresh_token
from werkzeug.security import generate_password_hash

from app import create_app
from app.config import TestConfig
from app.extensions import db
from app.models.customer import Customer
from app.models.employee import Employee
from app.models.garage import Garage
from app.models.mot_record import MOTRecord
from app.models.vehicle import Vehicle

DEFAULT_PASSWORD = "CorrectHorse123!"


# --------------------------------------------------------------------------
# Test database bootstrap
# --------------------------------------------------------------------------


def _ensure_database_exists(database_uri: str) -> None:
    """Create the target Postgres database if it doesn't already exist."""

    parts = urlsplit(database_uri.replace("+psycopg", ""))
    target_db = parts.path.lstrip("/")
    admin_uri = urlunsplit(parts._replace(path="/postgres"))

    conn = psycopg.connect(admin_uri, autocommit=True)
    try:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (target_db,)
        ).fetchone()
        if not exists:
            conn.execute(f'CREATE DATABASE "{target_db}"')
    finally:
        conn.close()


def _truncate_all_tables() -> None:
    table_names = [table.name for table in reversed(db.metadata.sorted_tables)]
    if not table_names:
        return
    db.session.execute(
        db.text(f"TRUNCATE TABLE {', '.join(table_names)} RESTART IDENTITY CASCADE")
    )
    db.session.commit()


@pytest.fixture(scope="session")
def _flask_app():
    _ensure_database_exists(TestConfig.SQLALCHEMY_DATABASE_URI)

    flask_app = create_app(TestConfig)

    with flask_app.app_context():
        db.create_all()

    yield flask_app

    with flask_app.app_context():
        db.session.remove()
        db.drop_all()
        db.engine.dispose()


@pytest.fixture()
def app(_flask_app):
    """The Flask app, with an app context pushed for the duration of the test.

    Tables are truncated after each test so every test starts from a clean,
    empty database regardless of what previous tests created.
    """

    ctx = _flask_app.app_context()
    ctx.push()

    yield _flask_app

    db.session.rollback()
    _truncate_all_tables()
    db.session.remove()
    ctx.pop()


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def session(app):
    return db.session


# --------------------------------------------------------------------------
# Authenticated HTTP client helper
# --------------------------------------------------------------------------


class AuthenticatedClient:
    """Thin wrapper around the Flask test client that injects a Bearer token."""

    def __init__(self, client, token):
        self._client = client
        self._headers = {"Authorization": f"Bearer {token}"}

    def _call(self, method, *args, **kwargs):
        headers = dict(kwargs.pop("headers", None) or {})
        headers.update(self._headers)
        kwargs["headers"] = headers
        return getattr(self._client, method)(*args, **kwargs)

    def get(self, *args, **kwargs):
        return self._call("get", *args, **kwargs)

    def post(self, *args, **kwargs):
        return self._call("post", *args, **kwargs)

    def patch(self, *args, **kwargs):
        return self._call("patch", *args, **kwargs)

    def delete(self, *args, **kwargs):
        return self._call("delete", *args, **kwargs)

    def put(self, *args, **kwargs):
        return self._call("put", *args, **kwargs)


# --------------------------------------------------------------------------
# Domain fixtures - Garage A / Employee A (the "primary" tenant)
# --------------------------------------------------------------------------


@pytest.fixture()
def garage(session):
    g = Garage(
        name="Garage A",
        email="contact@garage-a.example",
        phone="+44 20 7946 0001",
        address="1 Test Street, London",
    )
    session.add(g)
    session.commit()
    return g


@pytest.fixture()
def user(session, garage):
    u = Employee(
        garage_id=garage.id,
        email="owner-a@garage-a.example",
        password_hash=generate_password_hash(DEFAULT_PASSWORD),
        role="OWNER",
    )
    session.add(u)
    session.commit()
    return u


@pytest.fixture()
def access_token(app, user):
    return create_access_token(identity=str(user.id))


@pytest.fixture()
def refresh_token(app, user):
    return create_refresh_token(identity=str(user.id))


@pytest.fixture()
def authenticated_client(client, access_token):
    return AuthenticatedClient(client, access_token)


@pytest.fixture()
def authenticated_user(user, garage, access_token, refresh_token, authenticated_client):
    return SimpleNamespace(
        user=user,
        garage=garage,
        access_token=access_token,
        refresh_token=refresh_token,
        client=authenticated_client,
    )


@pytest.fixture()
def customer(session, garage):
    c = Customer(
        garage_id=garage.id,
        first_name="Jane",
        last_name="Doe",
        email="jane.doe@example.com",
        phone="+44 7700 900001",
    )
    session.add(c)
    session.commit()
    return c


@pytest.fixture()
def vehicle(session, garage, customer):
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
    return v


@pytest.fixture()
def mot_record(session, garage, vehicle):
    r = MOTRecord(
        garage_id=garage.id,
        vehicle_id=vehicle.id,
        mot_date=datetime.date(2026, 1, 1),
        expiry_date=datetime.date(2027, 1, 1),
        result="PASS",
        notes="All good.",
    )
    session.add(r)
    session.commit()
    vehicle.mot_expiry_date = r.expiry_date
    session.commit()
    return r


# --------------------------------------------------------------------------
# Domain fixtures - Garage B / Employee B (the "other" tenant, for isolation tests)
# --------------------------------------------------------------------------


@pytest.fixture()
def second_garage(session):
    g = Garage(
        name="Garage B",
        email="contact@garage-b.example",
        phone="+44 20 7946 0002",
        address="2 Test Street, Manchester",
    )
    session.add(g)
    session.commit()
    return g


@pytest.fixture()
def second_user(session, second_garage):
    u = Employee(
        garage_id=second_garage.id,
        email="owner-b@garage-b.example",
        password_hash=generate_password_hash(DEFAULT_PASSWORD),
        role="OWNER",
    )
    session.add(u)
    session.commit()
    return u


@pytest.fixture()
def second_access_token(app, second_user):
    return create_access_token(identity=str(second_user.id))


@pytest.fixture()
def second_authenticated_client(client, second_access_token):
    return AuthenticatedClient(client, second_access_token)


@pytest.fixture()
def second_customer(session, second_garage):
    c = Customer(
        garage_id=second_garage.id,
        first_name="John",
        last_name="Smith",
        email="john.smith@example.com",
        phone="+44 7700 900002",
    )
    session.add(c)
    session.commit()
    return c


@pytest.fixture()
def second_vehicle(session, second_garage, second_customer):
    v = Vehicle(
        garage_id=second_garage.id,
        customer_id=second_customer.id,
        registration_number="XY99ZZZ",
        make="Toyota",
        model="Corolla",
        year=2019,
        current_mileage=22000,
    )
    session.add(v)
    session.commit()
    return v


@pytest.fixture()
def second_mot_record(session, second_garage, second_vehicle):
    r = MOTRecord(
        garage_id=second_garage.id,
        vehicle_id=second_vehicle.id,
        mot_date=datetime.date(2026, 2, 1),
        expiry_date=datetime.date(2027, 2, 1),
        result="PASS",
        notes="Fine.",
    )
    session.add(r)
    session.commit()
    return r


# --------------------------------------------------------------------------
# Test-run result logging (test-results/test.log)
#
# Groups results into the same sections requested for the suite (Database
# Tests, Authentication, Garage API, Customer API, Vehicle API, MOT API) and
# writes a PASS/FAIL summary. Never records request/response payloads, so no
# passwords, tokens, or customer data end up in the log.
# --------------------------------------------------------------------------

RESULTS_DIR = Path(__file__).parent / "test-results"

_SECTION_BY_SUFFIX = [
    ("database/test_garage.py", "Database Tests"),
    ("database/test_employee.py", "Database Tests"),
    ("database/test_customer.py", "Database Tests"),
    ("database/test_vehicle.py", "Database Tests"),
    ("database/test_mot_record.py", "Database Tests"),
    ("database/test_isolation.py", "Database Tests"),
    ("api/test_auth.py", "Authentication"),
    ("api/test_garage.py", "Garage API"),
    ("api/test_customers.py", "Customer API"),
    ("api/test_vehicles.py", "Vehicle API"),
    ("api/test_mot_records.py", "MOT API"),
    ("test_health.py", "Health Check"),
]

_results = defaultdict(list)


def _section_for(nodeid: str) -> str:
    path = nodeid.split("::", 1)[0].replace(os.sep, "/")
    for suffix, title in _SECTION_BY_SUFFIX:
        if path.endswith(suffix):
            return title
    return path


def _label_for(nodeid: str) -> str:
    name = nodeid.split("::")[-1].split("[")[0]
    name = name.removeprefix("test_")
    return name.replace("_", " ")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()

    is_relevant = report.when == "call" or (
        report.when == "setup" and report.outcome != "passed"
    )
    if not is_relevant:
        return

    if report.outcome == "passed":
        status = "PASS"
    elif report.outcome == "skipped":
        status = "SKIP"
    else:
        status = "FAIL"

    _results[_section_for(report.nodeid)].append((_label_for(report.nodeid), status))


def pytest_sessionfinish(session, exitstatus):
    RESULTS_DIR.mkdir(exist_ok=True)

    lines = [f"Test Run: {datetime.datetime.now().astimezone():%Y-%m-%d %H:%M:%S}", ""]

    total = passed = failed = skipped = 0
    for section, entries in _results.items():
        lines.append(section)
        lines.append("-" * len(section))
        for label, status in entries:
            lines.append(f"{status} - {label}")
            total += 1
            if status == "PASS":
                passed += 1
            elif status == "FAIL":
                failed += 1
            else:
                skipped += 1
        lines.append("")

    lines.append("-" * 32)
    lines.append(f"TOTAL: {total}")
    lines.append(f"PASSED: {passed}")
    lines.append(f"FAILED: {failed}")
    if skipped:
        lines.append(f"SKIPPED: {skipped}")

    (RESULTS_DIR / "test.log").write_text("\n".join(lines) + "\n")
