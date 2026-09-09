"""Unit tests for the app/email service layer - template rendering, the
CommunicationLog write, duplicate-send guarding, and Resend failure handling.
Tested by importing and calling it directly (the same style as
tests/test_communications.py), with ``send_email`` itself replaced by a fake
so no real Resend/network call ever happens.
"""

import datetime

import pytest

from app.email import service as email_service
from app.models.appointments.appointment_checklist import AppointmentChecklist
from app.models.appointments.appointment_checklist_item import AppointmentChecklistItem
from app.models.communications.communication_log import CHANNEL_EMAIL, CommunicationLog


class _FakeSendEmail:
    """Records every call; raises ``exc`` instead of "sending" if given one."""

    def __init__(self, exc: Exception | None = None):
        self.exc = exc
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc is not None:
            raise self.exc


@pytest.fixture()
def fake_send(monkeypatch):
    fake = _FakeSendEmail()
    monkeypatch.setattr(email_service, "send_email", fake)
    return fake


def _add_checklist_item(session, checklist, *, label, visible, status="DONE", notes=None, order=0):
    item = AppointmentChecklistItem(
        garage_id=checklist.garage_id,
        appointment_checklist_id=checklist.id,
        order=order,
        label=label,
        status=status,
        notes=notes,
        visible_to_customer=visible,
    )
    session.add(item)
    session.commit()
    return item


# --------------------------------------------------------------------------
# Account created
# --------------------------------------------------------------------------


def test_account_created_email_is_sent_and_logged(app, session, fake_send, customer):
    from werkzeug.security import generate_password_hash

    customer.password_hash = generate_password_hash("SomeReal123!Password")
    session.commit()

    log = email_service.send_account_created_email(customer)

    assert log is not None
    assert log.status == email_service.STATUS_SENT
    assert log.channel == CHANNEL_EMAIL
    assert log.to_address == customer.email
    assert log.customer_id == customer.id
    assert len(fake_send.calls) == 1
    assert fake_send.calls[0]["to"] == customer.email
    # Never include the password (raw or hashed) - only the email it's tied to.
    assert "SomeReal123!Password" not in fake_send.calls[0]["body"]
    assert customer.password_hash not in fake_send.calls[0]["body"]
    assert customer.password_hash not in fake_send.calls[0]["html_body"]
    assert customer.email in fake_send.calls[0]["body"]


def test_account_created_email_skipped_with_no_address_on_file(app, fake_send, session, customer):
    customer.email = None
    session.commit()

    log = email_service.send_account_created_email(customer)

    assert log is None
    assert fake_send.calls == []
    assert CommunicationLog.query.count() == 0


# --------------------------------------------------------------------------
# Appointment confirmation
# --------------------------------------------------------------------------


def test_send_uses_business_name_and_owner_email_as_reply_to(
    app, fake_send, make_appointment, user
):
    """``user`` (see conftest.py) is the primary garage's OWNER - the send
    should read as coming from the business, with replies routed to its
    owner, never a bare address or the platform's own sending domain."""
    appointment = make_appointment(datetime.datetime(2026, 10, 1, 9, 0, tzinfo=datetime.UTC))

    log = email_service.send_appointment_confirmation_email(appointment)

    call = fake_send.calls[0]
    assert call["from_name"] == appointment.garage.name
    assert call["reply_to"] == user.email
    assert log.from_address == user.email


def test_owner_reply_to_falls_back_to_garage_email_with_no_owner(app, garage):
    # The bare `garage` fixture has no employees at all.
    assert email_service._owner_reply_to(garage) == garage.email


def test_appointment_confirmation_includes_appointment_details(
    app, fake_send, make_appointment, vehicle
):
    start = datetime.datetime(2026, 10, 1, 9, 0, tzinfo=datetime.UTC)
    appointment = make_appointment(start)
    appointment.vehicle_id = vehicle.id
    from app.extensions import db

    db.session.commit()

    log = email_service.send_appointment_confirmation_email(appointment)

    assert log is not None
    assert log.status == email_service.STATUS_SENT
    assert log.appointment_id == appointment.id
    body = fake_send.calls[0]["body"]
    assert vehicle.registration_number in body
    assert "01 October 2026" in body


def test_appointment_confirmation_skipped_with_no_customer_email(
    app, fake_send, session, make_appointment, customer
):
    customer.email = None
    session.commit()
    appointment = make_appointment(datetime.datetime(2026, 10, 1, 9, 0, tzinfo=datetime.UTC))

    log = email_service.send_appointment_confirmation_email(appointment)

    assert log is None
    assert fake_send.calls == []


def test_duplicate_confirmation_send_is_deduped(app, fake_send, make_appointment):
    appointment = make_appointment(datetime.datetime(2026, 10, 1, 9, 0, tzinfo=datetime.UTC))

    first = email_service.send_appointment_confirmation_email(appointment)
    second = email_service.send_appointment_confirmation_email(appointment)

    assert first is not None
    assert second is None
    assert len(fake_send.calls) == 1
    assert CommunicationLog.query.filter_by(appointment_id=appointment.id).count() == 1


# --------------------------------------------------------------------------
# Appointment changed
# --------------------------------------------------------------------------


def test_appointment_changed_shows_previous_and_new_time(app, fake_send, make_appointment):
    new_start = datetime.datetime(2026, 10, 2, 14, 0, tzinfo=datetime.UTC)
    appointment = make_appointment(new_start)
    previous_start = datetime.datetime(2026, 10, 1, 9, 0, tzinfo=datetime.UTC)

    log = email_service.send_appointment_changed_email(
        appointment,
        previous_start_time=previous_start,
        previous_end_time=previous_start + datetime.timedelta(hours=1),
    )

    assert log is not None
    body = fake_send.calls[0]["body"]
    assert "01 October 2026" in body
    assert "02 October 2026" in body


# --------------------------------------------------------------------------
# Appointment completed - checklist rendering
# --------------------------------------------------------------------------


def test_completed_email_only_includes_customer_visible_items(
    app, session, fake_send, make_appointment
):
    appointment = make_appointment(datetime.datetime(2026, 10, 1, 9, 0, tzinfo=datetime.UTC))
    appointment.notes = "Overall notes for this visit."
    appointment.status = "COMPLETED"
    checklist = AppointmentChecklist(garage_id=appointment.garage_id, appointment_id=appointment.id)
    session.add(checklist)
    session.commit()

    _add_checklist_item(
        session, checklist, label="Brake pads", visible=True, status="OK", notes="Fine", order=0
    )
    _add_checklist_item(
        session, checklist, label="Internal diagnostic code", visible=False, status="FAIL", order=1
    )

    log = email_service.send_appointment_completed_email(appointment)

    assert log is not None
    body = fake_send.calls[0]["body"]
    assert "Brake pads" in body
    assert "Overall notes for this visit." in body
    assert "Internal diagnostic code" not in body


def test_completed_email_handles_no_checklist(app, fake_send, make_appointment):
    appointment = make_appointment(datetime.datetime(2026, 10, 1, 9, 0, tzinfo=datetime.UTC))
    appointment.status = "COMPLETED"

    log = email_service.send_appointment_completed_email(appointment)

    assert log is not None
    assert len(fake_send.calls) == 1


# --------------------------------------------------------------------------
# Resend failure handling
# --------------------------------------------------------------------------


def test_send_failure_is_logged_and_handled_gracefully(app, monkeypatch, make_appointment, caplog):
    fake = _FakeSendEmail(exc=RuntimeError("Resend API returned 500"))
    monkeypatch.setattr(email_service, "send_email", fake)
    appointment = make_appointment(datetime.datetime(2026, 10, 1, 9, 0, tzinfo=datetime.UTC))

    log = email_service.send_appointment_confirmation_email(appointment)

    assert log is not None
    assert log.status == "FAILED"
    assert log.error_message == "Resend API returned 500"


def test_send_failure_never_leaks_the_api_key(app, monkeypatch, make_appointment):
    monkeypatch.setitem(app.config, "RESEND_API_KEY", "re_super_secret_value_123")
    fake = _FakeSendEmail(exc=RuntimeError("Resend API returned 500"))
    monkeypatch.setattr(email_service, "send_email", fake)
    appointment = make_appointment(datetime.datetime(2026, 10, 1, 9, 0, tzinfo=datetime.UTC))

    log = email_service.send_appointment_confirmation_email(appointment)

    assert "re_super_secret_value_123" not in (log.error_message or "")
    assert "re_super_secret_value_123" not in (log.body or "")
