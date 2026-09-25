"""Walk-in queue API (app/queueing): public join/status/cancel, the staff
lifecycle, timeouts, settings, and tenant isolation.

Time is frozen through ``app.queueing.service.utcnow`` - the one clock every
queue operation reads - to a fixed Monday. The garage is UTC with the default
schedule (Mon-Fri 09:00-17:00).
"""

import datetime

import pytest

from app.models.appointments.appointment import Appointment
from app.models.appointments.appointment_type import GarageAppointmentType
from app.models.customer import Customer
from app.models.employee import Employee
from app.models.queueing.queue_entry import QueueEntry
from app.models.queueing.queue_settings import GarageQueueSettings
from app.queueing import service

UTC = datetime.UTC
MONDAY = datetime.date(2026, 3, 2)
SATURDAY = datetime.date(2026, 3, 7)


def at(hour, minute=0, day=MONDAY):
    return datetime.datetime.combine(day, datetime.time(hour, minute), tzinfo=UTC)


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now += datetime.timedelta(**kwargs)


@pytest.fixture()
def clock(monkeypatch):
    c = Clock(at(10))
    monkeypatch.setattr(service, "utcnow", c)
    return c


@pytest.fixture()
def open_queue(session, garage, garage_schedule, appointment_type, clock):
    """Garage A, open, one bay (so ETAs are easy to reason about)."""
    garage_schedule.capacity_per_slot = 1
    session.add(GarageQueueSettings(garage_id=garage.id, is_open=True, no_show_timeout_minutes=10))
    session.commit()
    return garage


def join(client, slug="garage-a", **overrides):
    body = {
        "customer_first_name": "Sam",
        "customer_last_name": "Walker",
        "customer_phone": "07123 456789",
        "sms_opt_in": False,
        "vehicle_registration": "WK12 ABC",
    }
    body.update(overrides)
    return client.post(f"/api/public/{slug}/queue/join", json=body)


def status(client, token, slug="garage-a"):
    return client.post(f"/api/public/{slug}/queue/status", json={"token": token})


# --------------------------------------------------------------------------
# Public: info + join refusals
# --------------------------------------------------------------------------


def test_queue_is_closed_by_default(client, garage, garage_schedule, clock):
    resp = client.get("/api/public/garage-a/queue")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["is_open"] is False
    assert body["accepting_joins"] is False
    assert body["refusal_reason"] == "queue_closed"

    resp = join(client)
    assert resp.status_code == 409
    assert resp.get_json()["errors"]["reason"] == "queue_closed"


def test_staff_open_and_close_the_queue(
    authenticated_client, client, garage, garage_schedule, clock
):
    resp = authenticated_client.post("/api/queue/open")
    assert resp.status_code == 200
    assert resp.get_json()["is_open"] is True
    assert client.get("/api/public/garage-a/queue").get_json()["accepting_joins"] is True

    resp = authenticated_client.post("/api/queue/close")
    assert resp.get_json()["is_open"] is False
    assert join(client).status_code == 409


def test_unknown_garage_is_404(client, clock):
    assert client.get("/api/public/nope/queue").status_code == 404


def test_join_refused_on_a_closed_day(client, open_queue, clock):
    clock.now = at(10, day=SATURDAY)
    resp = join(client)
    assert resp.status_code == 409
    assert resp.get_json()["errors"]["reason"] == "closed_today"


def test_join_refused_after_closing(client, open_queue, clock):
    clock.now = at(17, 5)
    assert join(client).get_json()["errors"]["reason"] == "past_closing"


def test_join_refused_once_a_new_walk_in_could_not_finish_before_closing(client, open_queue, clock):
    # One bay, 60-minute default. At 16:10 a new walk-in would finish 17:10.
    clock.now = at(16, 10)
    resp = join(client)
    assert resp.status_code == 409
    assert resp.get_json()["errors"]["reason"] == "full_for_today"
    info = client.get("/api/public/garage-a/queue").get_json()
    assert info["accepting_joins"] is False
    assert info["refusal_reason"] == "full_for_today"


def test_join_refused_when_the_queue_ahead_fills_the_day(client, open_queue, clock):
    clock.now = at(14)
    for i in range(3):  # 14:00, 15:00, 16:00 - the last one ends at 17:00.
        assert join(client, customer_phone=f"07123 45670{i}").status_code == 201
    resp = join(client, customer_phone="07123 456799")
    assert resp.status_code == 409
    assert resp.get_json()["errors"]["reason"] == "full_for_today"


def test_join_rejects_a_landline(client, open_queue):
    resp = join(client, customer_phone="020 7946 0000")
    assert resp.status_code == 422


def test_same_number_cannot_join_twice(client, open_queue):
    assert join(client).status_code == 201
    resp = join(client, customer_phone="+447123456789")
    assert resp.status_code == 409
    assert resp.get_json()["errors"]["reason"] == "already_in_queue"


# --------------------------------------------------------------------------
# Public: join, status, cancel
# --------------------------------------------------------------------------


def test_join_returns_a_token_and_live_position(client, open_queue, session):
    resp = join(client)
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["token"]
    assert body["ticket_number"] == 1
    assert body["status"] == "WAITING"
    assert body["position"] == 1
    assert body["people_ahead"] == 0
    assert body["estimated_wait_minutes"] == 0
    assert body["fits_today"] is True

    entry = QueueEntry.query.one()
    # Stored normalised, and the token only as a hash.
    assert entry.customer_phone == "+447123456789"
    assert entry.public_token_hash != body["token"]
    assert entry.service_minutes == 60


def test_second_walk_in_waits_behind_the_first(client, open_queue):
    join(client)
    body = join(client, customer_phone="07123 456790").get_json()
    assert body["ticket_number"] == 2
    assert body["position"] == 2
    assert body["estimated_wait_minutes"] == 60


def test_eta_accounts_for_booked_appointments(client, open_queue, make_appointment, clock):
    # The single bay is booked 10:00-12:00, so a walk-in at 10:00 waits for it.
    make_appointment(at(10), minutes=120)
    body = join(client).get_json()
    assert body["estimated_wait_minutes"] == 120
    assert body["estimated_start_at"].startswith("2026-03-02T12:00")


def test_eta_uses_the_chosen_services_own_duration(client, open_queue, session):
    long = GarageAppointmentType(
        garage_id=open_queue.id, name="Full service", status="ACTIVE", default_duration_minutes=90
    )
    session.add(long)
    session.commit()
    join(client, appointment_type_id=str(long.id))
    body = join(client, customer_phone="07123 456790").get_json()
    assert body["estimated_wait_minutes"] == 90
    assert QueueEntry.query.filter_by(ticket_number=1).one().service_minutes == 90


def test_join_rejects_an_inactive_service(client, open_queue, session):
    hidden = GarageAppointmentType(garage_id=open_queue.id, name="Old", status="HIDDEN")
    session.add(hidden)
    session.commit()
    assert join(client, appointment_type_id=str(hidden.id)).status_code == 422


def test_status_refreshes_live_as_people_ahead_leave(client, open_queue):
    first = join(client).get_json()["token"]
    second = join(client, customer_phone="07123 456790").get_json()["token"]
    assert status(client, second).get_json()["position"] == 2

    resp = client.post("/api/public/garage-a/queue/cancel", json={"token": first})
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "CANCELLED"
    assert resp.get_json()["end_reason"] == "CUSTOMER_CANCELLED"

    body = status(client, second).get_json()
    assert body["position"] == 1
    assert body["estimated_wait_minutes"] == 0


def test_customer_cannot_cancel_twice(client, open_queue):
    token = join(client).get_json()["token"]
    client.post("/api/public/garage-a/queue/cancel", json={"token": token})
    resp = client.post("/api/public/garage-a/queue/cancel", json={"token": token})
    assert resp.status_code == 409


def test_unknown_token_is_404(client, open_queue):
    assert status(client, "x" * 32).status_code == 404


def test_token_is_scoped_to_its_garage(client, open_queue, second_garage):
    token = join(client).get_json()["token"]
    assert status(client, token, slug="garage-b").status_code == 404
    resp = client.post("/api/public/garage-b/queue/cancel", json={"token": token})
    assert resp.status_code == 404


def test_join_is_captcha_gated(client, open_queue, app, monkeypatch):
    monkeypatch.setattr("app.queueing.routes.verify_captcha", lambda token: False)
    assert join(client).status_code == 400
    assert QueueEntry.query.count() == 0


# --------------------------------------------------------------------------
# Staff lifecycle
# --------------------------------------------------------------------------


def test_happy_path_join_call_start_complete(
    client, authenticated_client, open_queue, user, clock, session
):
    token = join(client).get_json()["token"]

    dashboard = authenticated_client.get("/api/queue").get_json()
    assert [e["status"] for e in dashboard["entries"]] == ["WAITING"]
    assert dashboard["capacity"] == 1

    called = authenticated_client.post("/api/queue/call-next")
    assert called.status_code == 200
    entry_id = called.get_json()["id"]
    assert called.get_json()["status"] == "CALLED"
    assert called.get_json()["call_expires_at"].startswith("2026-03-02T10:10")
    # Calling someone forward creates no appointment yet.
    assert Appointment.query.count() == 0
    assert status(client, token).get_json()["status"] == "CALLED"

    clock.advance(minutes=3)
    started = authenticated_client.post(f"/api/queue/entries/{entry_id}/start", json={})
    assert started.status_code == 200
    assert started.get_json()["status"] == "IN_SERVICE"

    appointment = Appointment.query.one()
    assert appointment.status == "IN_PROGRESS"
    assert appointment.start_time == at(10, 3)
    assert appointment.end_time == at(11, 3)
    assert appointment.employee_id == user.id
    customer = Customer.query.filter_by(phone="+447123456789").one()
    assert appointment.customer_id == customer.id
    assert appointment.vehicle.registration_number == "WK12ABC"
    assert "ticket 1" in appointment.notes

    clock.advance(minutes=40)
    done = authenticated_client.post(f"/api/queue/entries/{entry_id}/complete")
    assert done.status_code == 200
    assert done.get_json()["status"] == "DONE"
    session.refresh(appointment)
    assert appointment.status == "COMPLETED"
    # The actual finish, not the estimate - this is what feeds the auto-average.
    assert appointment.end_time == at(10, 43)
    assert status(client, token).get_json()["status"] == "DONE"


def test_walk_in_in_service_holds_the_bay_for_people_behind(
    client, authenticated_client, open_queue, clock
):
    join(client)
    second = join(client, customer_phone="07123 456790").get_json()["token"]
    entry_id = authenticated_client.post("/api/queue/call-next").get_json()["id"]
    authenticated_client.post(f"/api/queue/entries/{entry_id}/start", json={})
    assert status(client, second).get_json()["estimated_wait_minutes"] == 60

    # The service overruns: the estimate moves back rather than claiming the
    # bay is already free.
    clock.advance(minutes=75)
    body = status(client, second).get_json()
    assert body["estimated_wait_minutes"] == 5


def test_start_service_reuses_an_existing_customer_by_phone(
    client, authenticated_client, open_queue, session
):
    existing = Customer(
        garage_id=open_queue.id, first_name="Sam", last_name="Old", phone="+447123456789"
    )
    session.add(existing)
    session.commit()
    join(client)
    entry_id = authenticated_client.post("/api/queue/call-next").get_json()["id"]
    authenticated_client.post(f"/api/queue/entries/{entry_id}/start", json={})
    assert Customer.query.count() == 1
    assert Appointment.query.one().customer_id == existing.id


def test_start_service_straight_from_waiting_with_explicit_choices(
    client, authenticated_client, open_queue, session, garage, staff_role
):
    colleague = Employee(
        garage_id=garage.id, email="mech@garage-a.example", password_hash="x", roles=[staff_role]
    )
    diag = GarageAppointmentType(
        garage_id=garage.id, name="Diagnostic", status="ACTIVE", default_duration_minutes=30
    )
    session.add_all([colleague, diag])
    session.commit()
    join(client)
    entry_id = QueueEntry.query.one().id
    resp = authenticated_client.post(
        f"/api/queue/entries/{entry_id}/start",
        json={"employee_id": str(colleague.id), "appointment_type_id": str(diag.id)},
    )
    assert resp.status_code == 200
    appointment = Appointment.query.one()
    assert appointment.employee_id == colleague.id
    assert appointment.appointment_type_id == diag.id
    assert (appointment.end_time - appointment.start_time) == datetime.timedelta(minutes=30)


def test_start_service_needs_some_active_service(
    client, authenticated_client, open_queue, appointment_type, session
):
    appointment_type.status = "HIDDEN"
    session.commit()
    join(client)
    entry_id = QueueEntry.query.one().id
    resp = authenticated_client.post(f"/api/queue/entries/{entry_id}/start", json={})
    assert resp.status_code == 422
    assert resp.get_json()["errors"]["reason"] == "no_appointment_type"
    assert QueueEntry.query.one().status == "WAITING"


def test_registration_clash_does_not_block_check_in(
    client, authenticated_client, open_queue, vehicle
):
    # AB12CDE already belongs to fixture customer Jane.
    join(client, vehicle_registration="AB12 CDE")
    entry_id = QueueEntry.query.one().id
    resp = authenticated_client.post(f"/api/queue/entries/{entry_id}/start", json={})
    assert resp.status_code == 200
    appointment = Appointment.query.one()
    assert appointment.vehicle_id is None
    assert "AB12 CDE" in appointment.notes


def test_staff_no_show_from_called_creates_no_appointment(client, authenticated_client, open_queue):
    join(client)
    entry_id = authenticated_client.post("/api/queue/call-next").get_json()["id"]
    resp = authenticated_client.post(f"/api/queue/entries/{entry_id}/no-show")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "NO_SHOW"
    assert resp.get_json()["end_reason"] == "STAFF_NO_SHOW"
    assert Appointment.query.count() == 0


def test_staff_cancel_and_invalid_transitions(client, authenticated_client, open_queue):
    join(client)
    entry_id = QueueEntry.query.one().id
    # Can't complete someone who hasn't started.
    assert authenticated_client.post(f"/api/queue/entries/{entry_id}/complete").status_code == 409
    resp = authenticated_client.post(f"/api/queue/entries/{entry_id}/cancel")
    assert resp.get_json()["end_reason"] == "STAFF_CANCELLED"
    assert authenticated_client.post(f"/api/queue/entries/{entry_id}/call").status_code == 409


def test_call_next_on_an_empty_queue(authenticated_client, open_queue):
    resp = authenticated_client.post("/api/queue/call-next")
    assert resp.status_code == 409
    assert resp.get_json()["errors"]["reason"] == "queue_empty"


def test_call_a_specific_entry_out_of_order(client, authenticated_client, open_queue):
    join(client)
    join(client, customer_phone="07123 456790")
    second = QueueEntry.query.filter_by(ticket_number=2).one()
    resp = authenticated_client.post(f"/api/queue/entries/{second.id}/call")
    assert resp.get_json()["status"] == "CALLED"
    assert QueueEntry.query.filter_by(ticket_number=1).one().status == "WAITING"


def test_reorder_moves_people_and_their_estimates(client, authenticated_client, open_queue):
    join(client)
    join(client, customer_phone="07123 456790")
    first = QueueEntry.query.filter_by(ticket_number=1).one()
    second = QueueEntry.query.filter_by(ticket_number=2).one()

    resp = authenticated_client.put(
        "/api/queue/order", json={"entry_ids": [str(second.id), str(first.id)]}
    )
    assert resp.status_code == 200
    by_ticket = {e["ticket_number"]: e for e in resp.get_json()["entries"]}
    assert by_ticket[2]["position"] == 1
    assert by_ticket[1]["position"] == 2
    assert by_ticket[1]["estimated_wait_minutes"] == 60


def test_reorder_with_a_stale_list_is_refused(client, authenticated_client, open_queue):
    join(client)
    join(client, customer_phone="07123 456790")
    first = QueueEntry.query.filter_by(ticket_number=1).one()
    resp = authenticated_client.put("/api/queue/order", json={"entry_ids": [str(first.id)]})
    assert resp.status_code == 409
    assert resp.get_json()["errors"]["reason"] == "stale_order"


def test_dashboard_shows_todays_appointments_on_the_same_timeline(
    client, authenticated_client, open_queue, make_appointment
):
    make_appointment(at(14), minutes=60)
    make_appointment(at(9), minutes=30, status="CANCELLED")
    join(client)
    body = authenticated_client.get("/api/queue").get_json()
    assert len(body["appointments"]) == 1
    appt = body["appointments"][0]
    assert appt["customer_name"] == "Jane Doe"
    assert appt["is_walk_in"] is False
    assert body["entries"][0]["estimated_wait_minutes"] == 0


# --------------------------------------------------------------------------
# No-show auto-skip timeout + day rollover
# --------------------------------------------------------------------------


def test_uncollected_call_times_out_and_the_next_person_is_called(
    client, authenticated_client, open_queue, clock
):
    join(client)
    second = join(client, customer_phone="07123 456790").get_json()["token"]
    first_id = authenticated_client.post("/api/queue/call-next").get_json()["id"]

    clock.advance(minutes=9)
    assert status(client, second).get_json()["status"] == "WAITING"

    clock.advance(minutes=1)
    # Any read runs the sweep - here, the customer's own poll.
    assert status(client, second).get_json()["status"] == "CALLED"
    first = QueueEntry.query.filter_by(id=first_id).one()
    assert first.status == "NO_SHOW"
    assert first.end_reason == "CALL_TIMEOUT"
    assert Appointment.query.count() == 0


def test_timeout_can_be_disabled(client, authenticated_client, open_queue, clock, session):
    GarageQueueSettings.query.one().no_show_timeout_minutes = None
    session.commit()
    join(client)
    entry_id = authenticated_client.post("/api/queue/call-next").get_json()["id"]
    clock.advance(hours=2)
    authenticated_client.get("/api/queue")
    assert QueueEntry.query.filter_by(id=entry_id).one().status == "CALLED"


def test_sweep_cli_times_out_calls_without_anyone_polling(
    client, authenticated_client, open_queue, clock, app
):
    join(client)
    join(client, customer_phone="07123 456790")
    authenticated_client.post("/api/queue/call-next")
    clock.advance(minutes=15)
    result = app.test_cli_runner().invoke(args=["sweep-walkin-queue"])
    assert "auto-called after a no-show timeout: 1" in result.output
    assert QueueEntry.query.filter_by(ticket_number=2).one().status == "CALLED"


def test_yesterdays_leftovers_are_ended_not_carried_over(
    client, authenticated_client, open_queue, clock
):
    join(client)
    clock.now = at(10, day=MONDAY + datetime.timedelta(days=1))
    body = authenticated_client.get("/api/queue").get_json()
    assert body["entries"] == []
    old = QueueEntry.query.one()
    assert old.status == "CANCELLED"
    assert old.end_reason == "DAY_ENDED"
    # And the ticket numbers start again.
    assert join(client).get_json()["ticket_number"] == 1


# --------------------------------------------------------------------------
# Settings + average time
# --------------------------------------------------------------------------


def test_settings_defaults_and_shared_capacity(
    authenticated_client, garage, garage_schedule, clock
):
    garage_schedule.capacity_per_slot = 3
    body = authenticated_client.get("/api/queue/settings").get_json()
    assert body["is_open"] is False
    assert body["average_mode"] == "AUTO"
    assert body["no_show_timeout_minutes"] == 10
    assert body["capacity"] == 3
    assert body["capacity_per_slot"] == 3
    assert body["average"] == {
        "effective_minutes": 60,
        "source": "DEFAULT",
        "auto_minutes": None,
        "auto_sample_size": 0,
    }


def test_auto_average_comes_from_completed_appointments(
    client, authenticated_client, open_queue, make_appointment, clock
):
    for day in range(1, 6):
        make_appointment(at(9) - datetime.timedelta(days=day), minutes=40, status="COMPLETED")
    # Not counted: not completed, and absurd data-entry noise.
    make_appointment(at(9) - datetime.timedelta(days=2), minutes=120, status="BOOKED")
    make_appointment(at(9) - datetime.timedelta(days=3), minutes=900, status="COMPLETED")

    body = authenticated_client.get("/api/queue/settings").get_json()
    assert body["average"]["source"] == "AUTO"
    assert body["average"]["effective_minutes"] == 40
    assert body["average"]["auto_sample_size"] == 5

    join(client)
    assert QueueEntry.query.one().service_minutes == 40


def test_auto_average_needs_enough_history(authenticated_client, open_queue, make_appointment):
    for day in range(1, 5):
        make_appointment(at(9) - datetime.timedelta(days=day), minutes=40, status="COMPLETED")
    body = authenticated_client.get("/api/queue/settings").get_json()
    assert body["average"]["source"] == "DEFAULT"
    assert body["average"]["auto_sample_size"] == 4


def test_manual_average_overrides_auto(client, authenticated_client, open_queue):
    resp = authenticated_client.put(
        "/api/queue/settings", json={"average_mode": "MANUAL", "manual_average_minutes": 25}
    )
    assert resp.status_code == 200
    assert resp.get_json()["average"]["source"] == "MANUAL"
    join(client)
    assert QueueEntry.query.one().service_minutes == 25


def test_manual_mode_requires_minutes(authenticated_client, open_queue):
    resp = authenticated_client.put("/api/queue/settings", json={"average_mode": "MANUAL"})
    assert resp.status_code == 422
    assert GarageQueueSettings.query.one().average_mode == "AUTO"


def test_default_service_must_belong_to_the_garage(
    authenticated_client, open_queue, second_garage, session
):
    foreign = GarageAppointmentType(garage_id=second_garage.id, name="B", status="ACTIVE")
    session.add(foreign)
    session.commit()
    resp = authenticated_client.put(
        "/api/queue/settings", json={"default_appointment_type_id": str(foreign.id)}
    )
    assert resp.status_code == 422


def test_settings_writes_are_owner_only(client, garage, staff_role, session, app, clock):
    from flask_jwt_extended import create_access_token

    staff = Employee(
        garage_id=garage.id, email="staff@garage-a.example", password_hash="x", roles=[staff_role]
    )
    session.add(staff)
    session.commit()
    headers = {"Authorization": f"Bearer {create_access_token(identity=str(staff.id))}"}
    assert client.get("/api/queue/settings", headers=headers).status_code == 200
    assert client.put("/api/queue/settings", json={}, headers=headers).status_code == 403
    # ...but operating the queue is for everyone.
    assert client.post("/api/queue/open", headers=headers).status_code == 200


# --------------------------------------------------------------------------
# SMS notifications
# --------------------------------------------------------------------------


@pytest.fixture()
def sent_sms(monkeypatch, app):
    from app.communications import sms_automation

    sent = []
    monkeypatch.setattr(sms_automation, "send_sms_message", lambda **kw: sent.append(kw))
    app.config["SMS_NOTIFICATIONS_ENABLED"] = True
    yield sent
    app.config["SMS_NOTIFICATIONS_ENABLED"] = False


def test_opted_in_walk_in_is_texted_on_join_and_when_called(
    client, authenticated_client, open_queue, sent_sms
):
    token = join(client, sms_opt_in=True).get_json()["token"]
    assert len(sent_sms) == 1
    assert sent_sms[0]["to"] == "+447123456789"
    # The link carries the token in the fragment, never the path or query.
    assert f"/queue/{open_queue.id}/status#{token}" in sent_sms[0]["body"]

    authenticated_client.post("/api/queue/call-next")
    assert len(sent_sms) == 2
    assert "your turn" in sent_sms[1]["body"]
    assert QueueEntry.query.one().called_sms_sent_at is not None


def test_not_opted_in_means_no_texts(client, authenticated_client, open_queue, sent_sms):
    join(client, sms_opt_in=False)
    authenticated_client.post("/api/queue/call-next")
    assert sent_sms == []


def test_auto_called_replacement_is_texted(
    client, authenticated_client, open_queue, sent_sms, clock
):
    join(client)
    join(client, customer_phone="07123 456790", sms_opt_in=True)
    authenticated_client.post("/api/queue/call-next")
    clock.advance(minutes=11)
    authenticated_client.get("/api/queue")
    assert [s["to"] for s in sent_sms] == ["+447123456790", "+447123456790"]


# --------------------------------------------------------------------------
# Tenant isolation
# --------------------------------------------------------------------------


def test_other_garage_staff_cannot_see_or_act_on_entries(
    client, open_queue, second_authenticated_client, second_garage
):
    join(client)
    entry_id = QueueEntry.query.one().id
    body = second_authenticated_client.get("/api/queue").get_json()
    assert body["entries"] == []
    for action in ("call", "start", "complete", "no-show", "cancel"):
        resp = second_authenticated_client.post(f"/api/queue/entries/{entry_id}/{action}", json={})
        assert resp.status_code == 404, action
    assert QueueEntry.query.one().status == "WAITING"
