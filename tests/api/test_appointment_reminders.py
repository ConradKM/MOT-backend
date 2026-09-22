"""Appointment reminders: settings CRUD, the automatic worker (idempotency,
cancellation exclusion, reschedule-safety), tenant isolation and disabled
communications channel fallback.
"""

import datetime

from app.appointment_reminders.defaults import (
    seed_appointment_reminder_settings,
)
from app.appointment_reminders.service import send_due_appointment_reminders
from app.models.appointment_reminder_settings import (
    AppointmentReminderSettings,
    AppointmentReminderTiming,
)
from app.models.reminder import STATUS_SENT, STATUS_SKIPPED, Reminder

UTC = datetime.UTC


def _in_hours(hours: float) -> datetime.datetime:
    return datetime.datetime.now(UTC) + datetime.timedelta(hours=hours)


def _enable(session, garage, *, timings=(24, 2), channels=("email",)):
    row = seed_appointment_reminder_settings(garage.id, session)
    row.enabled = True
    row.channels = list(channels)
    AppointmentReminderTiming.query.filter_by(settings_id=row.id).delete()
    session.flush()
    for hours in timings:
        session.add(AppointmentReminderTiming(settings_id=row.id, hours_before=hours, enabled=True))
    session.commit()
    return row


def _run_worker(session, garage, now=None):
    return send_due_appointment_reminders(session=session, garage_id=garage.id, now=now)


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------


def test_default_appointment_reminder_settings(authenticated_client):
    body = authenticated_client.get("/api/reminders/appointment-settings").get_json()
    assert body["enabled"] is False
    assert body["channels"] == ["email"]
    assert sorted(t["hours_before"] for t in body["timings"]) == [2, 24]
    assert body["available_channels"] == ["email"]


def test_owner_can_configure_appointment_reminders(authenticated_client, garage):
    resp = authenticated_client.put(
        "/api/reminders/appointment-settings",
        json={
            "enabled": True,
            "channels": ["email"],
            "timings": [
                {"hours_before": 48, "enabled": True},
                {"hours_before": 1, "enabled": True},
            ],
        },
    )
    assert resp.status_code == 200
    row = AppointmentReminderSettings.query.filter_by(garage_id=garage.id).one()
    assert row.enabled is True
    assert sorted(t.hours_before for t in row.timings) == [1, 48]


def test_duplicate_enabled_timings_rejected(authenticated_client):
    resp = authenticated_client.put(
        "/api/reminders/appointment-settings",
        json={
            "enabled": True,
            "channels": ["email"],
            "timings": [
                {"hours_before": 24, "enabled": True},
                {"hours_before": 24, "enabled": True},
            ],
        },
    )
    assert resp.status_code == 422


def test_aggregate_reminders_settings_endpoint(authenticated_client):
    body = authenticated_client.get("/api/reminders/settings").get_json()
    assert "mot" in body and "appointment" in body
    assert body["appointment"]["enabled"] is False
    assert body["mot"]["stage1_days_before"] == 30


# --------------------------------------------------------------------------
# Automatic worker
# --------------------------------------------------------------------------


def test_sends_due_reminder_by_email(session, garage, customer, make_appointment):
    _enable(session, garage)
    appt = make_appointment(_in_hours(24))

    created = _run_worker(session, garage)

    assert len(created) == 1
    reminder = created[0]
    assert reminder.status == STATUS_SENT
    assert reminder.channel == "email"
    assert reminder.type == "APPOINTMENT"
    assert reminder.appointment_id == appt.id


def test_not_yet_due_is_not_sent(session, garage, customer, make_appointment):
    _enable(session, garage)
    make_appointment(_in_hours(48))  # due at -24h, still 48h away

    created = _run_worker(session, garage)

    assert created == []


def test_running_worker_twice_does_not_duplicate(session, garage, customer, make_appointment):
    _enable(session, garage)
    make_appointment(_in_hours(24))

    first = _run_worker(session, garage)
    second = _run_worker(session, garage)

    assert len(first) == 1
    assert second == []
    assert Reminder.query.filter_by(type="APPOINTMENT").count() == 1


def test_multiple_timings_each_fire_once(session, garage, customer, make_appointment):
    _enable(session, garage, timings=(24, 2))
    make_appointment(_in_hours(1.5))  # both the 24h and 2h stages are already due

    created = _run_worker(session, garage)

    assert {r.stage for r in created} == {"H24", "H2"}


def test_disabled_settings_send_nothing(session, garage, customer, make_appointment):
    make_appointment(_in_hours(1))  # settings never enabled

    created = _run_worker(session, garage)

    assert created == []


def test_cancelled_appointment_is_not_reminded(session, garage, customer, make_appointment):
    _enable(session, garage)
    make_appointment(_in_hours(1), status="CANCELLED")

    created = _run_worker(session, garage)

    assert created == []


def test_completed_appointment_is_not_reminded(session, garage, customer, make_appointment):
    _enable(session, garage)
    make_appointment(_in_hours(1), status="COMPLETED")

    created = _run_worker(session, garage)

    assert created == []


def test_no_show_appointment_is_not_reminded(session, garage, customer, make_appointment):
    _enable(session, garage)
    make_appointment(_in_hours(1), status="NO_SHOW")

    created = _run_worker(session, garage)

    assert created == []


def test_unconfirmed_requested_appointment_is_not_reminded(
    session, garage, customer, make_appointment
):
    _enable(session, garage)
    make_appointment(_in_hours(1), status="REQUESTED")

    created = _run_worker(session, garage)

    assert created == []


def test_reschedule_does_not_resend_stale_reminder_but_schedules_a_fresh_one(
    session, garage, customer, make_appointment
):
    """Regression: rescheduling must not (a) fire a reminder for the old,
    no-longer-true time, and must not (b) get silently skipped for the new
    time because a reminder was "already sent" for that stage."""
    _enable(session, garage, timings=(24,))
    appt = make_appointment(_in_hours(24))

    first = _run_worker(session, garage)
    assert len(first) == 1
    assert first[0].scheduled_at == appt.start_time - datetime.timedelta(hours=24)

    # Reschedule further out - the 24h-before reminder for the new time isn't
    # due yet, so a fresh worker run must not fire (and must not error).
    appt.start_time = _in_hours(72)
    appt.end_time = appt.start_time + datetime.timedelta(hours=1)
    session.commit()

    not_yet = _run_worker(session, garage)
    assert not_yet == []

    # Move to the new due instant - now it should send, as a *new* row, not
    # be swallowed by the old (stale) "already sent" check.
    new_due_now = appt.start_time - datetime.timedelta(hours=24)
    second = _run_worker(session, garage, now=new_due_now)

    assert len(second) == 1
    assert second[0].scheduled_at == new_due_now
    assert Reminder.query.filter_by(appointment_id=appt.id, type="APPOINTMENT").count() == 2


def test_tenant_scoped_reminders_do_not_cross_garages(
    session, garage, customer, make_appointment, app
):
    from app.models.garage import Garage

    _enable(session, garage, timings=(24,))
    make_appointment(_in_hours(1))

    other_garage = Garage(name="Other Garage", slug="other-garage")
    session.add(other_garage)
    session.commit()

    created_for_other = send_due_appointment_reminders(session=session, garage_id=other_garage.id)
    assert created_for_other == []

    created_for_mine = _run_worker(session, garage)
    assert len(created_for_mine) == 1
    assert created_for_mine[0].garage_id == garage.id


def test_no_available_channel_records_skipped_not_pretend_sent(
    session, garage, customer, make_appointment
):
    """Owner selected SMS only, but this garage has no Twilio configuration -
    the worker must record a SKIPPED attempt, never claim delivery on a
    channel that isn't actually available."""
    _enable(session, garage, timings=(24,), channels=("sms",))
    make_appointment(_in_hours(1))

    created = _run_worker(session, garage)

    assert len(created) == 1
    assert created[0].status == STATUS_SKIPPED
    assert created[0].channel == "none"


def test_customer_with_no_email_is_skipped_not_silently_dropped(session, garage, make_appointment):
    from app.models.customer import Customer

    _enable(session, garage, timings=(24,))
    no_email_customer = Customer(
        garage_id=garage.id, first_name="Sam", last_name="NoContact", email=None, phone=None
    )
    session.add(no_email_customer)
    session.commit()

    appt = make_appointment(_in_hours(1))
    appt.customer_id = no_email_customer.id
    session.commit()

    created = _run_worker(session, garage)

    assert len(created) == 1
    assert created[0].status == STATUS_SKIPPED
