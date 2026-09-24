"""API tests for the expanded Appointment.status vocabulary.

REQUESTED/IN_PROGRESS/ACTION_NEEDED were added alongside BOOKED/COMPLETED/
CANCELLED/NO_SHOW to support the appointment overview + checklist UI.
"""

import pytest

from app.models.appointments.appointment import Appointment


def _make_appointment(authenticated_user, customer):
    appt_type = authenticated_user.client.post(
        "/api/appointment-types/", json={"name": "MOT"}
    ).get_json()

    appointment = authenticated_user.client.post(
        "/api/appointments/",
        json={
            "employee_id": str(authenticated_user.user.id),
            "customer_id": str(customer.id),
            "start_time": "2026-09-15T09:00:00+01:00",
            "end_time": "2026-09-15T10:00:00+01:00",
            "appointment_type_id": appt_type["id"],
        },
    ).get_json()

    return appointment


@pytest.mark.parametrize(
    "status", ["REQUESTED", "BOOKED", "IN_PROGRESS", "COMPLETED", "ACTION_NEEDED"]
)
def test_appointment_status_accepts_new_values(authenticated_user, customer, status):
    appointment = _make_appointment(authenticated_user, customer)

    resp = authenticated_user.client.patch(
        f"/api/appointments/{appointment['id']}", json={"status": status}
    )

    assert resp.status_code == 200
    assert resp.get_json()["status"] == status


def test_appointment_status_rejects_unknown_value(authenticated_user, customer):
    appointment = _make_appointment(authenticated_user, customer)

    resp = authenticated_user.client.patch(
        f"/api/appointments/{appointment['id']}", json={"status": "ON_HOLD"}
    )

    assert resp.status_code == 422


def test_new_appointment_defaults_to_booked(authenticated_user, customer):
    appointment = _make_appointment(authenticated_user, customer)

    assert appointment["status"] == "BOOKED"


@pytest.mark.parametrize("terminal_status", ["COMPLETED", "NO_SHOW"])
def test_terminal_appointment_cannot_be_reopened_by_a_stale_update(
    authenticated_user, customer, terminal_status
):
    appointment = _make_appointment(authenticated_user, customer)
    assert (
        authenticated_user.client.patch(
            f"/api/appointments/{appointment['id']}", json={"status": terminal_status}
        ).status_code
        == 200
    )

    response = authenticated_user.client.patch(
        f"/api/appointments/{appointment['id']}", json={"status": "BOOKED"}
    )

    assert response.status_code == 409
    assert Appointment.query.get(appointment["id"]).status == terminal_status


def test_cancelled_appointment_cannot_be_completed_without_reactivation(
    authenticated_user, customer
):
    appointment = _make_appointment(authenticated_user, customer)
    assert (
        authenticated_user.client.patch(
            f"/api/appointments/{appointment['id']}", json={"status": "CANCELLED"}
        ).status_code
        == 200
    )

    response = authenticated_user.client.patch(
        f"/api/appointments/{appointment['id']}", json={"status": "COMPLETED"}
    )

    assert response.status_code == 409
    assert Appointment.query.get(appointment["id"]).status == "CANCELLED"
