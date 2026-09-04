"""API tests for GET /api/mot-reminders (staff MOT reminder visibility)."""

import datetime

from app.models.reminder import Reminder

UTC = datetime.UTC


def _reminder(session, garage, vehicle, customer, **kw):
    defaults = {
        "garage_id": garage.id,
        "customer_id": customer.id,
        "vehicle_id": vehicle.id,
        "type": "MOT_DUE",
        "channel": "EMAIL",
        "scheduled_at": datetime.datetime.now(UTC) + datetime.timedelta(days=5),
        "status": "PENDING",
    }
    defaults.update(kw)
    r = Reminder(**defaults)
    session.add(r)
    session.commit()
    return r


def test_requires_auth(client):
    assert client.get("/api/mot-reminders/").status_code == 401


def test_only_lists_vehicles_with_an_mot_expiry(
    authenticated_client, session, garage, vehicle, customer, mot_record
):
    # `vehicle` has an expiry via the mot_record fixture; add one without.
    from app.models.vehicle import Vehicle

    session.add(
        Vehicle(
            garage_id=garage.id,
            customer_id=customer.id,
            registration_number="NO MOT",
            mot_expiry_date=None,
        )
    )
    session.commit()

    body = authenticated_client.get("/api/mot-reminders/").get_json()
    regs = {row["registration_number"] for row in body}
    assert vehicle.registration_number in regs
    assert "NOMOT" not in regs and "NO MOT" not in regs


def test_row_shape_and_not_scheduled_by_default(
    authenticated_client, garage, vehicle, customer, mot_record
):
    (row,) = authenticated_client.get("/api/mot-reminders/").get_json()
    assert row["registration_number"] == vehicle.registration_number
    assert row["customer_name"] == f"{customer.first_name} {customer.last_name}"
    assert row["mot_expiry_date"] == vehicle.mot_expiry_date.isoformat()
    assert row["reminder_status"] == "not_scheduled"
    assert row["last_reminder_sent"] is None
    assert row["next_reminder_scheduled"] is None


def test_scheduled_and_sent_status(
    authenticated_client, session, garage, vehicle, customer, mot_record
):
    _reminder(
        session,
        garage,
        vehicle,
        customer,
        status="SENT",
        sent_at=datetime.datetime.now(UTC) - datetime.timedelta(days=3),
        scheduled_at=datetime.datetime.now(UTC) - datetime.timedelta(days=3),
    )
    (row,) = authenticated_client.get("/api/mot-reminders/").get_json()
    assert row["reminder_status"] == "sent"
    assert row["last_reminder_sent"] is not None

    _reminder(session, garage, vehicle, customer)  # PENDING future
    (row,) = authenticated_client.get("/api/mot-reminders/").get_json()
    assert row["reminder_status"] == "scheduled"
    assert row["next_reminder_scheduled"] is not None


def test_non_mot_reminders_are_ignored(
    authenticated_client, session, garage, vehicle, customer, mot_record
):
    _reminder(session, garage, vehicle, customer, type="SERVICE_FOLLOW_UP")
    (row,) = authenticated_client.get("/api/mot-reminders/").get_json()
    assert row["reminder_status"] == "not_scheduled"


def test_tenant_scoped(
    authenticated_client,
    second_authenticated_client,
    session,
    second_garage,
    second_vehicle,
    second_customer,
):
    second_vehicle.mot_expiry_date = datetime.date(2027, 6, 1)
    session.commit()

    mine = authenticated_client.get("/api/mot-reminders/").get_json()
    theirs = second_authenticated_client.get("/api/mot-reminders/").get_json()
    assert all(
        row["registration_number"] != second_vehicle.registration_number
        for row in mine
    )
    assert any(
        row["registration_number"] == second_vehicle.registration_number
        for row in theirs
    )
