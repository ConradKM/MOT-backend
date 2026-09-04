"""API tests for the owner-facing schedule settings.

GET    /api/garage/schedule
PUT    /api/garage/schedule/settings
PUT    /api/garage/schedule/opening-hours
POST   /api/garage/schedule/exceptions
DELETE /api/garage/schedule/exceptions/<id>
"""

import datetime

from flask_jwt_extended import create_access_token
from werkzeug.security import generate_password_hash

from app.models.employee import Employee

FUTURE = (datetime.date.today() + datetime.timedelta(days=20)).isoformat()


def test_get_returns_seeded_defaults(authenticated_client):
    body = authenticated_client.get("/api/garage/schedule").get_json()

    assert body["settings"]["slot_interval_minutes"] == 30
    assert body["settings"]["max_advance_days"] == 60
    assert len(body["opening_hours"]) == 7
    assert body["exceptions"] == []


def test_put_settings_updates_values(authenticated_client):
    resp = authenticated_client.put(
        "/api/garage/schedule/settings",
        json={"slot_interval_minutes": 15, "capacity_per_slot": 4},
    )
    assert resp.status_code == 200
    assert resp.get_json()["slot_interval_minutes"] == 15

    body = authenticated_client.get("/api/garage/schedule").get_json()
    assert body["settings"]["slot_interval_minutes"] == 15
    assert body["settings"]["capacity_per_slot"] == 4


def test_put_settings_rejects_out_of_range(authenticated_client):
    resp = authenticated_client.put(
        "/api/garage/schedule/settings", json={"slot_interval_minutes": 0}
    )
    assert resp.status_code == 422


def test_put_opening_hours_replaces_weekday(authenticated_client):
    resp = authenticated_client.put(
        "/api/garage/schedule/opening-hours",
        json={
            "opening_hours": [
                {
                    "weekday": 5,
                    "opens_at": "10:00",
                    "closes_at": "14:00",
                    "is_closed": False,
                }
            ]
        },
    )
    assert resp.status_code == 200

    hours = {h["weekday"]: h for h in resp.get_json()["opening_hours"]}
    assert hours[5]["is_closed"] is False
    assert hours[5]["opens_at"].startswith("10:00")
    assert hours[5]["closes_at"].startswith("14:00")


def test_put_opening_hours_rejects_bad_range(authenticated_client):
    resp = authenticated_client.put(
        "/api/garage/schedule/opening-hours",
        json={
            "opening_hours": [
                {
                    "weekday": 1,
                    "opens_at": "17:00",
                    "closes_at": "09:00",
                    "is_closed": False,
                }
            ]
        },
    )
    assert resp.status_code == 422


def test_add_and_delete_exception(authenticated_client):
    created = authenticated_client.post(
        "/api/garage/schedule/exceptions",
        json={"date": FUTURE, "is_closed": True, "note": "Bank holiday"},
    )
    assert created.status_code == 201
    exc_id = created.get_json()["id"]

    body = authenticated_client.get("/api/garage/schedule").get_json()
    assert [e["date"] for e in body["exceptions"]] == [FUTURE]

    assert (
        authenticated_client.delete(
            f"/api/garage/schedule/exceptions/{exc_id}"
        ).status_code
        == 204
    )
    body = authenticated_client.get("/api/garage/schedule").get_json()
    assert body["exceptions"] == []


def test_add_exception_duplicate_date_returns_409(authenticated_client):
    payload = {"date": FUTURE, "is_closed": True}
    assert (
        authenticated_client.post(
            "/api/garage/schedule/exceptions", json=payload
        ).status_code
        == 201
    )
    assert (
        authenticated_client.post(
            "/api/garage/schedule/exceptions", json=payload
        ).status_code
        == 409
    )


def test_non_owner_cannot_write(client, session, garage, staff_role):
    emp = Employee(
        garage_id=garage.id,
        email="staff-a@garage-a.example",
        password_hash=generate_password_hash("x"),
        roles=[staff_role],
    )
    session.add(emp)
    session.commit()
    token = create_access_token(identity=str(emp.id))

    resp = client.put(
        "/api/garage/schedule/settings",
        json={"slot_interval_minutes": 15},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


def test_schedule_is_tenant_scoped(
    authenticated_client, second_authenticated_client
):
    created = authenticated_client.post(
        "/api/garage/schedule/exceptions",
        json={"date": FUTURE, "is_closed": True},
    )
    exc_id = created.get_json()["id"]

    other = second_authenticated_client.get("/api/garage/schedule").get_json()
    assert other["exceptions"] == []
    assert (
        second_authenticated_client.delete(
            f"/api/garage/schedule/exceptions/{exc_id}"
        ).status_code
        == 404
    )
