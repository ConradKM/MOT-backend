"""Model-level behaviour of AddOn and its per-booking snapshots."""

import datetime

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.appointments.add_on import AddOn
from app.models.appointments.applied_add_on import AppointmentAddOn
from app.models.appointments.appointment import Appointment
from app.models.appointments.appointment_type import GarageAppointmentType


@pytest.fixture()
def appt_type(session, garage):
    t = GarageAppointmentType(garage_id=garage.id, name="MOT", status="ACTIVE")
    session.add(t)
    session.commit()
    return t


def _add_on(session, appt_type, **fields):
    a = AddOn(
        garage_id=appt_type.garage_id, appointment_type_id=appt_type.id, name="Wash", **fields
    )
    session.add(a)
    session.commit()
    return a


def test_defaults(session, appt_type):
    a = _add_on(session, appt_type)

    assert str(a.price_delta) == "0.00"
    assert a.duration_delta_minutes == 0
    assert a.max_quantity == 1
    assert a.exclusivity_group is None
    assert a.status == "ACTIVE"


def test_max_quantity_check_constraint(session, appt_type):
    with pytest.raises(IntegrityError):
        _add_on(session, appt_type, max_quantity=100)
    session.rollback()


def test_deleting_the_type_cascades_to_its_add_ons(session, appt_type):
    _add_on(session, appt_type)

    session.delete(appt_type)
    session.commit()

    assert AddOn.query.count() == 0


def test_deleting_an_add_on_keeps_applied_snapshots(session, garage, appt_type, user, customer):
    a = _add_on(session, appt_type, price_delta="12.50", duration_delta_minutes=20)
    start = datetime.datetime(2026, 12, 1, 9, tzinfo=datetime.UTC)
    appt = Appointment(
        garage_id=garage.id,
        employee_id=user.id,
        customer_id=customer.id,
        appointment_type_id=appt_type.id,
        start_time=start,
        end_time=start + datetime.timedelta(hours=1),
        add_ons=[
            AppointmentAddOn(
                garage_id=garage.id,
                add_on_id=a.id,
                name=a.name,
                quantity=2,
                price_delta=a.price_delta,
                duration_delta_minutes=a.duration_delta_minutes,
            )
        ],
    )
    session.add(appt)
    session.commit()

    session.delete(a)
    session.commit()
    session.refresh(appt)

    [row] = appt.add_ons
    assert row.add_on_id is None
    assert row.name == "Wash"
    assert str(row.price_delta) == "12.50"
    assert row.quantity == 2
