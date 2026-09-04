"""MOT reminders: the visibility page, the configurable schedule, the
automatic worker (booking suppression + no double-sends) and the manual
"Send reminder" action.

Covers ConradKM/MOT-backend#31.
"""

import datetime

import pytest

from app.models.appointments.appointment import Appointment
from app.models.mot_reminder_settings import MOTReminderSettings
from app.models.reminder import (
    STAGE_1,
    STAGE_2,
    STAGE_3,
    STAGE_MANUAL,
    TRIGGER_AUTOMATIC,
    TRIGGER_MANUAL,
    Reminder,
)
from app.mot_reminders.service import send_due_automatic_reminders

UTC = datetime.UTC
TODAY = datetime.datetime.now(UTC).date()


def _expiry_in(days: int) -> datetime.date:
    return TODAY + datetime.timedelta(days=days)


def _set_expiry(session, vehicle, days: int):
    vehicle.mot_expiry_date = _expiry_in(days)
    session.commit()


def _an_employee(session, garage):
    from werkzeug.security import generate_password_hash

    from app.models.employee import Employee

    emp = Employee.query.filter_by(garage_id=garage.id).first()
    if emp is None:
        emp = Employee(
            garage_id=garage.id,
            email=f"tech-{garage.id}@example.test",
            password_hash=generate_password_hash("CorrectHorse123!"),
        )
        session.add(emp)
        session.commit()
    return emp


def _book_mot(session, garage, vehicle, customer, appt_type, *, start_days, status="BOOKED"):
    start = datetime.datetime.combine(
        _expiry_in(start_days), datetime.time(10, 0), tzinfo=UTC
    )
    appt = Appointment(
        garage_id=garage.id,
        employee_id=_an_employee(session, garage).id,
        customer_id=customer.id,
        vehicle_id=vehicle.id,
        appointment_type_id=appt_type.id,
        start_time=start,
        end_time=start + datetime.timedelta(hours=1),
        status=status,
    )
    session.add(appt)
    session.commit()
    return appt


def _run_worker(session, garage):
    return send_due_automatic_reminders(session=session, garage_id=garage.id)


# --------------------------------------------------------------------------
# Reminder schedule settings
# --------------------------------------------------------------------------


def test_default_schedule_is_30_7_1(authenticated_client):
    body = authenticated_client.get("/api/mot-reminders/settings").get_json()
    assert body["stage1_days_before"] == 30
    assert body["stage2_days_before"] == 7
    assert body["stage3_days_before"] == 1
    assert body["stage1_enabled"] and body["stage2_enabled"] and body["stage3_enabled"]


def test_owner_can_change_reminder_intervals(authenticated_client, session, garage):
    resp = authenticated_client.put(
        "/api/mot-reminders/settings",
        json={
            "stage1_days_before": 21,
            "stage2_days_before": 3,
            "stage3_days_before": 1,
        },
    )
    assert resp.status_code == 200
    row = MOTReminderSettings.query.filter_by(garage_id=garage.id).one()
    assert (row.stage1_days_before, row.stage2_days_before, row.stage3_days_before) == (21, 3, 1)


def test_owner_can_disable_a_stage(authenticated_client, garage):
    resp = authenticated_client.put(
        "/api/mot-reminders/settings", json={"stage2_enabled": False}
    )
    assert resp.status_code == 200
    assert resp.get_json()["stage2_enabled"] is False


def test_intervals_are_normalised_furthest_out_first(authenticated_client):
    body = authenticated_client.put(
        "/api/mot-reminders/settings",
        json={
            "stage1_days_before": 1,
            "stage2_days_before": 30,
            "stage3_days_before": 7,
        },
    ).get_json()
    assert [body["stage1_days_before"], body["stage2_days_before"], body["stage3_days_before"]] == [
        30,
        7,
        1,
    ]


def test_zero_or_negative_interval_is_rejected(authenticated_client):
    assert (
        authenticated_client.put(
            "/api/mot-reminders/settings", json={"stage3_days_before": 0}
        ).status_code
        == 422
    )
    assert (
        authenticated_client.put(
            "/api/mot-reminders/settings", json={"stage3_days_before": -5}
        ).status_code
        == 422
    )


def test_duplicate_enabled_intervals_are_rejected(authenticated_client):
    resp = authenticated_client.put(
        "/api/mot-reminders/settings",
        json={
            "stage1_days_before": 7,
            "stage2_days_before": 7,
            "stage3_days_before": 1,
        },
    )
    assert resp.status_code == 422


def test_staff_cannot_change_the_schedule(authenticated_user, session):
    authenticated_user.user.roles = []
    session.commit()
    resp = authenticated_user.client.put(
        "/api/mot-reminders/settings", json={"stage1_days_before": 21}
    )
    assert resp.status_code == 403


def test_schedule_is_tenant_isolated(
    authenticated_client, second_authenticated_client
):
    authenticated_client.put(
        "/api/mot-reminders/settings", json={"stage1_days_before": 45}
    )
    other = second_authenticated_client.get("/api/mot-reminders/settings").get_json()
    assert other["stage1_days_before"] == 30


# --------------------------------------------------------------------------
# Automatic worker
# --------------------------------------------------------------------------


def test_first_reminder_sends_once_and_only_once(
    session, garage, vehicle, customer, mot_record
):
    _set_expiry(session, vehicle, 20)  # stage1 (30d) is due

    first = _run_worker(session, garage)
    assert len(first) == 1
    assert first[0].stage == STAGE_1
    assert first[0].trigger == TRIGGER_AUTOMATIC
    assert first[0].status == "SENT"

    again = _run_worker(session, garage)
    assert again == []
    assert Reminder.query.filter_by(vehicle_id=vehicle.id, stage=STAGE_1).count() == 1


def test_second_reminder_sends_at_the_configured_interval(
    session, garage, vehicle, customer, mot_record
):
    settings = MOTReminderSettings(
        garage_id=garage.id,
        stage1_enabled=False, stage1_days_before=30,
        stage2_enabled=True, stage2_days_before=7,
        stage3_enabled=False, stage3_days_before=1,
    )
    session.add(settings)
    _set_expiry(session, vehicle, 7)  # exactly the stage2 window opens today

    sent = _run_worker(session, garage)
    assert [r.stage for r in sent] == [STAGE_2]


def test_final_reminder_sends_at_the_configured_interval(
    session, garage, vehicle, customer, mot_record
):
    settings = MOTReminderSettings(
        garage_id=garage.id,
        stage1_enabled=False, stage2_enabled=False,
        stage3_enabled=True, stage3_days_before=1,
    )
    session.add(settings)
    _set_expiry(session, vehicle, 1)

    sent = _run_worker(session, garage)
    assert [r.stage for r in sent] == [STAGE_3]


def test_expired_mot_gets_no_pre_expiry_reminders(
    session, garage, vehicle, customer, mot_record
):
    _set_expiry(session, vehicle, -1)
    assert _run_worker(session, garage) == []


def test_customer_with_no_booking_stays_eligible(
    session, garage, vehicle, customer, mot_record
):
    _set_expiry(session, vehicle, 3)
    assert len(_run_worker(session, garage)) >= 1


def test_active_mot_booking_stops_automatic_reminders(
    session, garage, vehicle, customer, mot_record, appointment_type
):
    _set_expiry(session, vehicle, 20)
    _book_mot(session, garage, vehicle, customer, appointment_type, start_days=5)

    assert _run_worker(session, garage) == []
    assert Reminder.query.filter_by(vehicle_id=vehicle.id).count() == 0


def test_booking_a_different_vehicle_does_not_suppress(
    session, garage, customer, appointment_type
):
    from app.models.vehicle import Vehicle

    audi = Vehicle(
        garage_id=garage.id, customer_id=customer.id,
        registration_number="OB08AUD", make="Audi", model="A4",
        mot_expiry_date=_expiry_in(20),
    )
    focus = Vehicle(
        garage_id=garage.id, customer_id=customer.id,
        registration_number="OB09FOC", make="Ford", model="Focus",
        mot_expiry_date=_expiry_in(200),
    )
    session.add_all([audi, focus])
    session.commit()

    _book_mot(session, garage, focus, customer, appointment_type, start_days=3)

    sent = _run_worker(session, garage)
    assert [r.vehicle_id for r in sent] == [audi.id]


def test_cancelled_booking_restores_eligibility(
    session, garage, vehicle, customer, mot_record, appointment_type
):
    _set_expiry(session, vehicle, 20)
    appt = _book_mot(
        session, garage, vehicle, customer, appointment_type, start_days=5
    )
    assert _run_worker(session, garage) == []

    appt.status = "CANCELLED"
    session.commit()

    sent = _run_worker(session, garage)
    assert [r.stage for r in sent] == [STAGE_1]


@pytest.mark.parametrize("status", ["CANCELLED", "NO_SHOW"])
def test_cancelled_or_no_show_bookings_never_suppress(
    session, garage, vehicle, customer, mot_record, appointment_type, status
):
    _set_expiry(session, vehicle, 20)
    _book_mot(
        session, garage, vehicle, customer, appointment_type,
        start_days=5, status=status,
    )
    assert len(_run_worker(session, garage)) == 1


# --------------------------------------------------------------------------
# The staff page
# --------------------------------------------------------------------------


def test_row_shape_and_next_scheduled(
    authenticated_client, session, garage, vehicle, customer, mot_record
):
    _set_expiry(session, vehicle, 100)  # nothing due yet

    (row,) = authenticated_client.get("/api/mot-reminders/").get_json()
    assert row["registration_number"] == vehicle.registration_number
    assert row["customer_name"] == f"{customer.first_name} {customer.last_name}"
    assert row["reminder_status"] == "scheduled"
    assert row["last_reminder_sent"] is None
    # next eligible reminder = expiry - 30 days (stage 1)
    assert row["next_reminder_scheduled"] == (
        vehicle.mot_expiry_date - datetime.timedelta(days=30)
    ).isoformat()
    assert [s["stage"] for s in row["stages"]] == [STAGE_1, STAGE_2, STAGE_3]


def test_last_sent_and_history_after_a_send(
    authenticated_client, session, garage, vehicle, customer, mot_record
):
    _set_expiry(session, vehicle, 20)
    _run_worker(session, garage)

    (row,) = authenticated_client.get("/api/mot-reminders/").get_json()
    assert row["last_reminder_sent"] is not None
    assert any(h["stage"] == STAGE_1 and h["trigger"] == TRIGGER_AUTOMATIC for h in row["history"])
    stage1 = next(s for s in row["stages"] if s["stage"] == STAGE_1)
    assert stage1["state"] == "sent"


def test_next_scheduled_clears_when_a_booking_exists(
    authenticated_client, session, garage, vehicle, customer, mot_record, appointment_type
):
    _set_expiry(session, vehicle, 20)
    _book_mot(session, garage, vehicle, customer, appointment_type, start_days=5)

    (row,) = authenticated_client.get("/api/mot-reminders/").get_json()
    assert row["booking_active"] is True
    assert row["reminder_status"] == "booked"
    assert row["next_reminder_scheduled"] is None


def test_page_is_tenant_scoped(
    authenticated_client, second_authenticated_client, session,
    second_garage, second_vehicle, second_customer,
):
    second_vehicle.mot_expiry_date = datetime.date(2027, 6, 1)
    session.commit()

    mine = authenticated_client.get("/api/mot-reminders/").get_json()
    theirs = second_authenticated_client.get("/api/mot-reminders/").get_json()
    assert all(r["registration_number"] != second_vehicle.registration_number for r in mine)
    assert any(r["registration_number"] == second_vehicle.registration_number for r in theirs)


# --------------------------------------------------------------------------
# Manual "Send reminder"
# --------------------------------------------------------------------------


def test_manual_reminder_can_be_sent(
    authenticated_client, session, garage, vehicle, customer, mot_record
):
    _set_expiry(session, vehicle, 100)
    resp = authenticated_client.post(f"/api/mot-reminders/{vehicle.id}/send", json={})
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["stage"] == STAGE_MANUAL
    assert body["trigger"] == TRIGGER_MANUAL


def test_manual_reminder_is_logged_with_the_initiator(
    authenticated_user, session, garage, vehicle, customer, mot_record
):
    authenticated_user.client.post(f"/api/mot-reminders/{vehicle.id}/send", json={})

    row = Reminder.query.filter_by(vehicle_id=vehicle.id, stage=STAGE_MANUAL).one()
    assert row.trigger == TRIGGER_MANUAL
    assert row.initiated_by_employee_id == authenticated_user.user.id


def test_manual_reminder_is_blocked_when_a_booking_exists(
    authenticated_client, session, garage, vehicle, customer, mot_record, appointment_type
):
    _set_expiry(session, vehicle, 20)
    _book_mot(session, garage, vehicle, customer, appointment_type, start_days=5)

    blocked = authenticated_client.post(f"/api/mot-reminders/{vehicle.id}/send", json={})
    assert blocked.status_code == 409
    assert Reminder.query.filter_by(vehicle_id=vehicle.id).count() == 0

    forced = authenticated_client.post(
        f"/api/mot-reminders/{vehicle.id}/send", json={"acknowledge_booking": True}
    )
    assert forced.status_code == 201
    assert Reminder.query.filter_by(vehicle_id=vehicle.id, stage=STAGE_MANUAL).count() == 1


def test_manual_send_rejects_a_foreign_vehicle(
    authenticated_client, second_vehicle
):
    resp = authenticated_client.post(
        f"/api/mot-reminders/{second_vehicle.id}/send", json={}
    )
    assert resp.status_code == 404


def test_manual_reminder_shows_in_history_separately_from_automatic(
    authenticated_client, session, garage, vehicle, customer, mot_record
):
    _set_expiry(session, vehicle, 20)
    _run_worker(session, garage)  # automatic STAGE_1
    authenticated_client.post(f"/api/mot-reminders/{vehicle.id}/send", json={})

    (row,) = authenticated_client.get("/api/mot-reminders/").get_json()
    triggers = {h["trigger"] for h in row["history"]}
    assert triggers == {TRIGGER_AUTOMATIC, TRIGGER_MANUAL}
