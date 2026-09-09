"""API-level tests confirming appointment lifecycle actions (create, update,
complete) trigger the right automatic email - through the real event bus
(app/communications/events.py -> app/email/automation.py), not by calling the
email service directly. Rendering/dedupe/Resend-failure handling itself is
covered by tests/test_email_service.py; the app/email/automation.py handler
functions are monkeypatched to a capture box here so these stay fast and
never touch a real provider.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.models.appointments.appointment import Appointment
from app.models.appointments.appointment_type import GarageAppointmentType

START = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)
END = START + timedelta(minutes=30)


@pytest.fixture()
def appt_type(session, garage):
    t = GarageAppointmentType(garage_id=garage.id, name="MOT", status="ACTIVE")
    session.add(t)
    session.commit()
    return t


@pytest.fixture()
def appointment(session, garage, appt_type, authenticated_user, customer):
    appt = Appointment(
        garage_id=garage.id,
        employee_id=authenticated_user.user.id,
        customer_id=customer.id,
        appointment_type_id=appt_type.id,
        start_time=START,
        end_time=END,
        status="BOOKED",
    )
    session.add(appt)
    session.commit()
    return appt


def _capture(monkeypatch, name):
    box = {}
    monkeypatch.setattr(
        f"app.email.automation.{name}",
        lambda appointment, **kw: box.update(appointment_id=str(appointment.id), **kw),
    )
    return box


# --------------------------------------------------------------------------
# Creation -> confirmation email
# --------------------------------------------------------------------------


def test_creating_appointment_triggers_confirmation_email(
    authenticated_user, customer, appt_type, monkeypatch
):
    box = _capture(monkeypatch, "send_appointment_confirmation_email")

    resp = authenticated_user.client.post(
        "/api/appointments/",
        json={
            "employee_id": str(authenticated_user.user.id),
            "customer_id": str(customer.id),
            "appointment_type_id": str(appt_type.id),
            "start_time": START.isoformat(),
            "end_time": END.isoformat(),
        },
    )
    assert resp.status_code == 201
    assert box["appointment_id"] == resp.get_json()["id"]


def test_failed_appointment_creation_does_not_trigger_email(
    authenticated_user, customer, appt_type, monkeypatch
):
    box = _capture(monkeypatch, "send_appointment_confirmation_email")

    resp = authenticated_user.client.post(
        "/api/appointments/",
        json={
            "employee_id": str(authenticated_user.user.id),
            "customer_id": str(customer.id),
            "appointment_type_id": str(appt_type.id),
            "start_time": END.isoformat(),  # start after end -> 422, never created
            "end_time": START.isoformat(),
        },
    )
    assert resp.status_code == 422
    assert box == {}
    assert Appointment.query.count() == 0


# --------------------------------------------------------------------------
# Reschedule -> changed email
# --------------------------------------------------------------------------


def test_rescheduling_appointment_triggers_changed_email(
    authenticated_user, appointment, monkeypatch
):
    box = _capture(monkeypatch, "send_appointment_changed_email")

    new_start = START + timedelta(days=1)
    new_end = new_start + timedelta(minutes=30)
    resp = authenticated_user.client.patch(
        f"/api/appointments/{appointment.id}",
        json={"start_time": new_start.isoformat(), "end_time": new_end.isoformat()},
    )

    assert resp.status_code == 200
    assert box["appointment_id"] == str(appointment.id)
    assert box["previous_start_time"] == START
    assert box["previous_end_time"] == END


def test_failed_appointment_update_does_not_trigger_email(
    authenticated_user, appointment, monkeypatch
):
    box = _capture(monkeypatch, "send_appointment_changed_email")

    resp = authenticated_user.client.patch(
        f"/api/appointments/{appointment.id}",
        json={"start_time": END.isoformat(), "end_time": START.isoformat()},  # invalid range
    )

    assert resp.status_code == 422
    assert box == {}


# --------------------------------------------------------------------------
# Completion -> summary email
# --------------------------------------------------------------------------


def test_completing_appointment_triggers_completed_email(
    authenticated_user, appointment, monkeypatch
):
    box = _capture(monkeypatch, "send_appointment_completed_email")

    resp = authenticated_user.client.patch(
        f"/api/appointments/{appointment.id}", json={"status": "COMPLETED"}
    )

    assert resp.status_code == 200
    assert box["appointment_id"] == str(appointment.id)


def test_re_saving_an_already_completed_appointment_does_not_refire(
    authenticated_user, session, appointment, monkeypatch
):
    appointment.status = "COMPLETED"
    session.commit()
    box = _capture(monkeypatch, "send_appointment_completed_email")

    resp = authenticated_user.client.patch(
        f"/api/appointments/{appointment.id}", json={"notes": "Updated notes only"}
    )

    assert resp.status_code == 200
    assert box == {}
