"""Guards on who/what an appointment can be assigned to.

Covers ConradKM/MOT-backend hardening: a deactivated employee can't be newly
assigned to an appointment (existing appointments keep their employee), and
an archived customer/vehicle is reactivated rather than silently left
inaccessible once a new appointment is booked for them.
"""

from datetime import UTC, datetime, timedelta

from werkzeug.security import generate_password_hash

from app.models.appointments.appointment import Appointment
from app.models.appointments.appointment_type import GarageAppointmentType
from app.models.employee import Employee

START = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)
END = START + timedelta(minutes=30)


def _appt_type(session, garage, base_price=None):
    t = GarageAppointmentType(
        garage_id=garage.id, name="MOT", status="ACTIVE", base_price=base_price
    )
    session.add(t)
    session.commit()
    return t


def _deactivated_employee(session, garage):
    emp = Employee(
        garage_id=garage.id,
        email="gone@garage-a.example",
        password_hash=generate_password_hash("CorrectHorse123!"),
        is_active=False,
    )
    session.add(emp)
    session.commit()
    return emp


def test_cannot_create_an_appointment_for_a_deactivated_employee(
    authenticated_user, session, garage, customer
):
    appt_type = _appt_type(session, garage)
    deactivated = _deactivated_employee(session, garage)

    resp = authenticated_user.client.post(
        "/api/appointments/",
        json={
            "employee_id": str(deactivated.id),
            "customer_id": str(customer.id),
            "appointment_type_id": str(appt_type.id),
            "start_time": START.isoformat(),
            "end_time": END.isoformat(),
        },
    )
    assert resp.status_code == 422
    assert Appointment.query.count() == 0


def test_cannot_reassign_an_appointment_to_a_deactivated_employee(
    authenticated_user, session, garage, customer
):
    appt_type = _appt_type(session, garage)
    appt = authenticated_user.client.post(
        "/api/appointments/",
        json={
            "employee_id": str(authenticated_user.user.id),
            "customer_id": str(customer.id),
            "appointment_type_id": str(appt_type.id),
            "start_time": START.isoformat(),
            "end_time": END.isoformat(),
        },
    ).get_json()
    deactivated = _deactivated_employee(session, garage)

    resp = authenticated_user.client.patch(
        f"/api/appointments/{appt['id']}", json={"employee_id": str(deactivated.id)}
    )
    assert resp.status_code == 422


def test_existing_appointment_survives_its_employee_being_deactivated_later(
    authenticated_user, session, garage, customer
):
    appt_type = _appt_type(session, garage)
    other = Employee(
        garage_id=garage.id,
        email="still-here@garage-a.example",
        password_hash=generate_password_hash("CorrectHorse123!"),
    )
    session.add(other)
    session.commit()

    appt = authenticated_user.client.post(
        "/api/appointments/",
        json={
            "employee_id": str(other.id),
            "customer_id": str(customer.id),
            "appointment_type_id": str(appt_type.id),
            "start_time": START.isoformat(),
            "end_time": END.isoformat(),
        },
    ).get_json()

    other.is_active = False
    session.commit()

    # Untouched fields on an existing appointment don't re-validate the
    # employee - only assigning/changing one does.
    resp = authenticated_user.client.patch(
        f"/api/appointments/{appt['id']}", json={"notes": "Customer running late."}
    )
    assert resp.status_code == 200
    assert resp.get_json()["employee_id"] == str(other.id)


def test_booking_an_archived_customer_reactivates_them(
    authenticated_user, session, garage, customer
):
    appt_type = _appt_type(session, garage)
    customer.is_active = False
    session.commit()

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
    session.refresh(customer)
    assert customer.is_active is True


def test_appointment_snapshots_the_types_price_at_creation(
    authenticated_user, session, garage, customer
):
    appt_type = _appt_type(session, garage, base_price="149.99")

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
    assert resp.get_json()["price_at_booking"] == "149.99"

    # The type's price changing later doesn't rewrite history (item 16).
    appt_type.base_price = "199.99"
    session.commit()
    appt_id = resp.get_json()["id"]
    refetched = authenticated_user.client.get(f"/api/appointments/{appt_id}").get_json()
    assert refetched["price_at_booking"] == "149.99"


def test_booking_an_archived_vehicle_reactivates_it(
    authenticated_user, session, garage, customer, vehicle
):
    appt_type = _appt_type(session, garage)
    vehicle.is_active = False
    session.commit()

    resp = authenticated_user.client.post(
        "/api/appointments/",
        json={
            "employee_id": str(authenticated_user.user.id),
            "customer_id": str(customer.id),
            "vehicle_id": str(vehicle.id),
            "appointment_type_id": str(appt_type.id),
            "start_time": START.isoformat(),
            "end_time": END.isoformat(),
        },
    )
    assert resp.status_code == 201
    session.refresh(vehicle)
    assert vehicle.is_active is True
