"""Business Terms & Conditions (app/terms/): the settings API, the public
pages, and enforcement on every web booking path (plain, deposit, their
retry/recovery paths, and the walk-in queue).

The conversational AWAITING_TERMS flow lives in test_terms_conversation.py.
"""

import datetime
import uuid

import pytest
from flask_jwt_extended import create_access_token
from werkzeug.security import generate_password_hash

from app.models.appointments.appointment_type import GarageAppointmentType
from app.models.booking_request import BookingRequest
from app.models.employee import Employee
from app.models.garage_terms_version import GarageTermsVersion
from app.models.payments.payment import BookingPayment
from app.models.queueing.queue_entry import QueueEntry
from app.models.queueing.queue_settings import GarageQueueSettings
from app.models.role import Role
from app.queueing import service as queue_service
from app.terms.service import save_terms

TERMS = "1. Deposits are non-refundable.\n2. Please arrive 10 minutes early."


def _future_weekday(days_ahead=7):
    d = datetime.datetime.now(datetime.UTC).date() + datetime.timedelta(days=days_ahead)
    while d.weekday() >= 5:
        d += datetime.timedelta(days=1)
    return d


FUTURE_DATE = _future_weekday().isoformat()


def _payload(**overrides):
    payload = {
        "customer_first_name": "Alex",
        "customer_last_name": "Turner",
        "customer_email": "alex.turner@example.com",
        "customer_phone": "07123 456789",
        "vehicle_registration": "PB11 REQ",
        "preferred_date": FUTURE_DATE,
        "preferred_time": "09:30:00",
    }
    payload.update(overrides)
    return payload


def _deposit_type(session, garage):
    t = GarageAppointmentType(
        garage_id=garage.id,
        name="Full Service",
        status="ACTIVE",
        base_price="100.00",
        deposit_required=True,
        deposit_type="FIXED",
        deposit_value="20.00",
    )
    session.add(t)
    session.commit()
    return t


@pytest.fixture()
def with_terms(session, garage):
    save_terms(garage, TERMS)
    return garage


@pytest.fixture()
def staff_client(client, session, garage):
    role = Role(garage_id=garage.id, name="STAFF")
    staff = Employee(
        garage_id=garage.id,
        email="staff@garage-a.example",
        password_hash=generate_password_hash("CorrectHorse123!"),
        roles=[role],
    )
    session.add_all([role, staff])
    session.commit()
    token = create_access_token(identity=str(staff.id))
    return lambda method, url, **kw: getattr(client, method)(
        url, headers={"Authorization": f"Bearer {token}"}, **kw
    )


# --------------------------------------------------------------------------
# Settings API
# --------------------------------------------------------------------------


def test_owner_saves_terms_creating_version_one(authenticated_client, garage, user):
    resp = authenticated_client.put(
        "/api/garage/terms", json={"terms_and_conditions": f"  {TERMS}\n\n"}
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["terms_and_conditions"] == TERMS  # stored trimmed
    assert body["terms_version"] == 1
    assert body["terms_updated_at"] is not None

    row = GarageTermsVersion.query.filter_by(garage_id=garage.id).one()
    assert (row.version, row.body, row.created_by_employee_id) == (1, TERMS, user.id)


def test_saving_unchanged_text_does_not_create_a_version(authenticated_client, garage):
    authenticated_client.put("/api/garage/terms", json={"terms_and_conditions": TERMS})
    resp = authenticated_client.put(
        "/api/garage/terms", json={"terms_and_conditions": f"\n{TERMS}   "}
    )
    assert resp.get_json()["terms_version"] == 1
    assert GarageTermsVersion.query.filter_by(garage_id=garage.id).count() == 1


def test_a_real_change_bumps_the_version_and_keeps_the_old_wording(
    authenticated_client, session, garage
):
    authenticated_client.put("/api/garage/terms", json={"terms_and_conditions": TERMS})
    resp = authenticated_client.put("/api/garage/terms", json={"terms_and_conditions": "New."})
    assert resp.get_json()["terms_version"] == 2

    rows = GarageTermsVersion.query.filter_by(garage_id=garage.id).order_by("version").all()
    assert [(r.version, r.body) for r in rows] == [(1, TERMS), (2, "New.")]


def test_versions_are_immutable(session, with_terms):
    row = GarageTermsVersion.query.one()
    row.body = "rewritten history"
    with pytest.raises(ValueError, match="immutable"):
        session.commit()
    session.rollback()
    assert GarageTermsVersion.query.one().body == TERMS


@pytest.mark.parametrize("empty", ["", "   \n\t ", None])
def test_empty_text_turns_terms_off(authenticated_client, client, garage, empty):
    authenticated_client.put("/api/garage/terms", json={"terms_and_conditions": TERMS})
    resp = authenticated_client.put("/api/garage/terms", json={"terms_and_conditions": empty})
    assert resp.status_code == 200
    assert resp.get_json()["terms_and_conditions"] is None
    assert resp.get_json()["terms_version"] is None

    public = client.get(f"/api/public/{garage.slug}").get_json()
    assert public["terms_and_conditions"] is None
    assert public["terms_version"] is None
    # The old wording is still on record for anyone who accepted it.
    assert GarageTermsVersion.query.filter_by(garage_id=garage.id).count() == 1


def test_switching_back_on_with_the_same_wording_reuses_its_version(authenticated_client):
    authenticated_client.put("/api/garage/terms", json={"terms_and_conditions": TERMS})
    authenticated_client.put("/api/garage/terms", json={"terms_and_conditions": ""})
    resp = authenticated_client.put("/api/garage/terms", json={"terms_and_conditions": TERMS})
    assert resp.get_json()["terms_version"] == 1


def test_terms_over_the_length_cap_are_rejected(authenticated_client, garage):
    resp = authenticated_client.put(
        "/api/garage/terms", json={"terms_and_conditions": "x" * 50_001}
    )
    assert resp.status_code == 422
    assert "terms_and_conditions" in resp.get_json()["errors"]["json"]
    assert GarageTermsVersion.query.count() == 0


def test_staff_can_read_but_only_the_owner_can_change_terms(staff_client, with_terms):
    assert staff_client("get", "/api/garage/terms").get_json()["terms_and_conditions"] == TERMS
    resp = staff_client("put", "/api/garage/terms", json={"terms_and_conditions": "Mine now"})
    assert resp.status_code == 403
    assert GarageTermsVersion.query.count() == 1


def test_settings_are_scoped_to_the_callers_business(
    authenticated_client, second_authenticated_client, second_garage, with_terms
):
    assert second_authenticated_client.get("/api/garage/terms").get_json() == {
        "terms_and_conditions": None,
        "terms_version": None,
        "terms_updated_at": None,
    }
    second_authenticated_client.put("/api/garage/terms", json={"terms_and_conditions": "B's"})
    assert authenticated_client.get("/api/garage/terms").get_json()["terms_and_conditions"] == TERMS


def test_settings_require_authentication(client):
    assert client.get("/api/garage/terms").status_code == 401


# --------------------------------------------------------------------------
# Public payload and Terms pages
# --------------------------------------------------------------------------


def test_public_payload_carries_the_terms_on_both_entry_points(client, with_terms):
    by_slug = client.get(f"/api/public/{with_terms.slug}").get_json()
    by_id = client.get(f"/api/public/garages/{with_terms.id}").get_json()
    for payload in (by_slug, by_id):
        assert payload["terms_and_conditions"] == TERMS
        assert payload["terms_version"] == 1


def test_public_payload_without_terms_is_null(client, garage):
    payload = client.get(f"/api/public/{garage.slug}").get_json()
    assert payload["terms_and_conditions"] is None
    assert payload["terms_version"] is None


def test_public_terms_page_serves_the_current_wording(client, with_terms):
    resp = client.get(f"/api/public/garages/{with_terms.id}/terms")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["body"] == TERMS
    assert body["version"] == 1
    assert body["garage_name"] == with_terms.name
    assert body["is_current"] is True
    assert "max-age" in resp.headers["Cache-Control"]


def test_versioned_terms_page_keeps_serving_what_was_accepted(client, with_terms):
    save_terms(with_terms, "Changed wording")
    old = client.get(f"/api/public/garages/{with_terms.id}/terms/1")
    assert old.status_code == 200
    assert old.get_json()["body"] == TERMS
    assert old.get_json()["is_current"] is False
    assert "immutable" in old.headers["Cache-Control"]
    assert client.get(f"/api/public/garages/{with_terms.id}/terms").get_json()["version"] == 2

    # Still there after the business switches terms off entirely.
    save_terms(with_terms, "")
    assert client.get(f"/api/public/garages/{with_terms.id}/terms").status_code == 404
    assert client.get(f"/api/public/garages/{with_terms.id}/terms/1").status_code == 200


def test_public_terms_404s(client, garage, with_terms, second_garage):
    assert client.get(f"/api/public/garages/{garage.id}/terms/99").status_code == 404
    assert client.get(f"/api/public/garages/{uuid.uuid4()}/terms").status_code == 404
    # A garage only ever exposes its own terms: B has none, so A's version 1
    # is not reachable through B's URL.
    assert client.get(f"/api/public/garages/{second_garage.id}/terms").status_code == 404
    assert client.get(f"/api/public/garages/{second_garage.id}/terms/1").status_code == 404


def test_suspended_business_terms_are_not_public(client, session, with_terms):
    with_terms.status = "SUSPENDED"
    session.commit()
    assert client.get(f"/api/public/garages/{with_terms.id}/terms").status_code == 404


# --------------------------------------------------------------------------
# Plain web booking
# --------------------------------------------------------------------------


def test_business_without_terms_books_exactly_as_before(client, garage, garage_schedule):
    resp = client.post(f"/api/public/{garage.slug}/booking-requests", json=_payload())
    assert resp.status_code == 201
    booking = BookingRequest.query.one()
    assert booking.terms_accepted_at is None
    assert booking.terms_version is None


def test_booking_without_acceptance_is_rejected(client, with_terms, garage_schedule):
    resp = client.post(f"/api/public/{with_terms.slug}/booking-requests", json=_payload())
    assert resp.status_code == 422
    assert "terms_accepted" in resp.get_json()["errors"]["json"]

    resp = client.post(
        f"/api/public/{with_terms.slug}/booking-requests",
        json=_payload(terms_accepted=False, terms_version=1),
    )
    assert resp.status_code == 422
    assert BookingRequest.query.count() == 0


def test_booking_against_a_stale_version_is_a_distinguishable_conflict(
    client, with_terms, garage_schedule
):
    save_terms(with_terms, "Updated while the customer was booking")
    resp = client.post(
        f"/api/public/{with_terms.slug}/booking-requests",
        json=_payload(terms_accepted=True, terms_version=1),
    )
    assert resp.status_code == 409
    assert resp.get_json()["errors"]["reason"] == "terms_version_mismatch"
    assert BookingRequest.query.count() == 0


def test_accepted_booking_records_the_version_with_a_server_timestamp(
    client, with_terms, garage_schedule
):
    before = datetime.datetime.now(datetime.UTC)
    resp = client.post(
        f"/api/public/{with_terms.slug}/booking-requests",
        json=_payload(
            terms_accepted=True,
            terms_version=1,
            # Never trusted - not even a field the schema reads.
            terms_accepted_at="2001-01-01T00:00:00Z",
        ),
    )
    assert resp.status_code == 201
    booking = BookingRequest.query.one()
    assert booking.status == "PENDING"
    assert booking.terms_version == 1
    assert booking.terms_accepted_at >= before
    assert booking.source == "WEB"


def test_retry_returns_the_same_request_without_restamping_or_bypassing(
    client, with_terms, garage_schedule
):
    attempt = str(uuid.uuid4())
    url = f"/api/public/{with_terms.slug}/booking-requests"
    first = client.post(
        url, json=_payload(payment_attempt_id=attempt, terms_accepted=True, terms_version=1)
    )
    stamped = BookingRequest.query.one().terms_accepted_at

    # Replaying the attempt id is no way around acceptance.
    assert client.post(url, json=_payload(payment_attempt_id=attempt)).status_code == 422

    again = client.post(
        url, json=_payload(payment_attempt_id=attempt, terms_accepted=True, terms_version=1)
    )
    assert again.status_code == 201
    assert again.get_json()["id"] == first.get_json()["id"]
    assert BookingRequest.query.count() == 1
    assert BookingRequest.query.one().terms_accepted_at == stamped


def test_tokenless_duplicate_fallback_also_requires_acceptance(client, with_terms, garage_schedule):
    url = f"/api/public/{with_terms.slug}/booking-requests"
    first = client.post(url, json=_payload(terms_accepted=True, terms_version=1))
    stamped = BookingRequest.query.one().terms_accepted_at

    assert client.post(url, json=_payload()).status_code == 422
    again = client.post(url, json=_payload(terms_accepted=True, terms_version=1))
    assert again.get_json()["id"] == first.get_json()["id"]
    assert BookingRequest.query.one().terms_accepted_at == stamped


# --------------------------------------------------------------------------
# Deposit web booking
# --------------------------------------------------------------------------


def test_deposit_intent_requires_acceptance_before_any_hold_or_payment(
    client, session, with_terms, garage_schedule
):
    appt_type = _deposit_type(session, with_terms)
    resp = client.post(
        f"/api/public/{with_terms.slug}/booking-requests/deposit-intent",
        json=_payload(appointment_type_id=str(appt_type.id)),
    )
    assert resp.status_code == 422
    assert "terms_accepted" in resp.get_json()["errors"]["json"]
    assert BookingRequest.query.count() == 0
    assert BookingPayment.query.count() == 0


def test_deposit_intent_rejects_a_stale_version(client, session, with_terms, garage_schedule):
    appt_type = _deposit_type(session, with_terms)
    save_terms(with_terms, "v2")
    resp = client.post(
        f"/api/public/{with_terms.slug}/booking-requests/deposit-intent",
        json=_payload(appointment_type_id=str(appt_type.id), terms_accepted=True, terms_version=1),
    )
    assert resp.status_code == 409
    assert resp.get_json()["errors"]["reason"] == "terms_version_mismatch"
    assert BookingPayment.query.count() == 0


def test_deposit_intent_stamps_acceptance_and_a_resume_does_not_restamp(
    client, session, with_terms, garage_schedule
):
    appt_type = _deposit_type(session, with_terms)
    url = f"/api/public/{with_terms.slug}/booking-requests/deposit-intent"
    body = _payload(
        appointment_type_id=str(appt_type.id),
        payment_attempt_id=str(uuid.uuid4()),
        terms_accepted=True,
        terms_version=1,
    )
    first = client.post(url, json=body)
    assert first.status_code == 201
    booking = BookingRequest.query.one()
    assert booking.status == "AWAITING_PAYMENT"
    assert booking.terms_version == 1
    stamped = booking.terms_accepted_at

    # A resume of the same attempt still has to carry acceptance...
    without = {k: v for k, v in body.items() if not k.startswith("terms_")}
    assert client.post(url, json=without).status_code == 422
    # ...and returns the same hold without stamping it again.
    resumed = client.post(url, json=body)
    assert resumed.status_code == 201
    assert resumed.get_json()["booking_request_id"] == first.get_json()["booking_request_id"]
    session.expire_all()
    assert BookingRequest.query.one().terms_accepted_at == stamped

    # Recovery reads the server snapshot; it neither needs nor writes acceptance.
    recovered = client.post(
        f"/api/public/{with_terms.slug}/booking-requests/deposit-attempt/recover",
        json={"recovery_token": first.get_json()["recovery_token"]},
    )
    assert recovered.status_code == 200
    session.expire_all()
    assert BookingRequest.query.one().terms_accepted_at == stamped


# --------------------------------------------------------------------------
# Walk-in queue
# --------------------------------------------------------------------------


@pytest.fixture()
def open_queue(monkeypatch, session, garage, garage_schedule, appointment_type):
    monday = datetime.date(2026, 3, 2)
    monkeypatch.setattr(
        queue_service,
        "utcnow",
        lambda: datetime.datetime.combine(monday, datetime.time(10), tzinfo=datetime.UTC),
    )
    session.add(GarageQueueSettings(garage_id=garage.id, is_open=True))
    session.commit()
    return garage


def _join(client, slug, **overrides):
    body = {"customer_first_name": "Sam", "customer_phone": "07123 456789"}
    body.update(overrides)
    return client.post(f"/api/public/{slug}/queue/join", json=body)


def test_queue_join_without_terms_is_unchanged(client, open_queue):
    assert _join(client, open_queue.slug).status_code == 201
    assert QueueEntry.query.one().terms_accepted_at is None


def test_queue_join_enforces_and_records_acceptance(client, open_queue):
    save_terms(open_queue, TERMS)
    missing = _join(client, open_queue.slug)
    assert missing.status_code == 422
    assert "terms_accepted" in missing.get_json()["errors"]["json"]

    stale = _join(client, open_queue.slug, terms_accepted=True, terms_version=7)
    assert stale.status_code == 409
    assert stale.get_json()["errors"]["reason"] == "terms_version_mismatch"
    assert QueueEntry.query.count() == 0

    ok = _join(client, open_queue.slug, terms_accepted=True, terms_version=1)
    assert ok.status_code == 201
    entry = QueueEntry.query.one()
    assert entry.terms_version == 1
    assert entry.terms_accepted_at is not None


# --------------------------------------------------------------------------
# Staff review + customer portal
# --------------------------------------------------------------------------


def test_staff_review_shows_what_was_accepted(
    client, authenticated_client, with_terms, garage_schedule
):
    client.post(
        f"/api/public/{with_terms.slug}/booking-requests",
        json=_payload(terms_accepted=True, terms_version=1),
    )
    [row] = authenticated_client.get("/api/booking-requests/").get_json()
    assert row["terms_version"] == 1
    assert row["terms_accepted_at"] is not None
    assert row["terms_url"].endswith(f"/terms/{with_terms.id}/v/1")


def test_customer_portal_links_the_business_terms(customer_client, session, garage, customer):
    assert (
        customer_client.get("/api/customer/account").get_json()["customer"]["garage_terms_url"]
        is None
    )
    save_terms(garage, TERMS)
    account = customer_client.get("/api/customer/account").get_json()
    assert account["customer"]["garage_terms_url"].endswith(f"/terms/{garage.id}")
