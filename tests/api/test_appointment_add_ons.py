"""API tests for appointment add-ons.

Configuration:  /api/appointment-types/<id>/add-ons[/<add_on_id>]
Selection:      staff POST/PATCH /api/appointments/, public booking submit,
                deposit intent, availability, and staff approval.
"""

import datetime

from app.extensions import db
from app.models.appointments.appointment import Appointment
from app.models.appointments.appointment_type import GarageAppointmentType
from app.models.booking_request import BookingRequest
from app.models.payments.payment import BookingPayment

START = "2026-12-01T09:00:00+00:00"


def _future_weekday(days_ahead=7):
    d = datetime.datetime.now(datetime.UTC).date() + datetime.timedelta(days=days_ahead)
    while d.weekday() >= 5:
        d += datetime.timedelta(days=1)
    return d


FUTURE_DATE = _future_weekday().isoformat()


def _type(client, **fields):
    body = {"name": "Service", "base_price": "100.00", "default_duration_minutes": 60}
    body.update(fields)
    resp = client.post("/api/appointment-types/", json=body)
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()


def _add_on(client, type_id, **fields):
    body = {"name": "Extra", "price_delta": "15.00", "duration_delta_minutes": 30}
    body.update(fields)
    resp = client.post(f"/api/appointment-types/{type_id}/add-ons", json=body)
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()


def _appointment(staff, customer, type_id, add_ons=(), **overrides):
    payload = {
        "employee_id": str(staff.user.id),
        "customer_id": str(customer.id),
        "appointment_type_id": type_id,
        "start_time": START,
        "add_ons": list(add_ons),
    }
    payload.update(overrides)
    return staff.client.post("/api/appointments/", json=payload)


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def test_owner_can_create_list_update_and_delete_add_ons(authenticated_user):
    client = authenticated_user.client
    t = _type(client)

    created = _add_on(
        client,
        t["id"],
        name="Rush job",
        price_delta="-5.50",
        duration_delta_minutes=-15,
        max_quantity=3,
        exclusivity_group="Speed",
    )
    assert created["price_delta"] == "-5.50"
    assert created["duration_delta_minutes"] == -15
    assert created["max_quantity"] == 3
    assert created["exclusivity_group"] == "Speed"
    assert created["status"] == "ACTIVE"

    listed = client.get(f"/api/appointment-types/{t['id']}/add-ons").get_json()
    assert [a["id"] for a in listed] == [created["id"]]

    patched = client.patch(
        f"/api/appointment-types/{t['id']}/add-ons/{created['id']}",
        json={"price_delta": "7.00", "exclusivity_group": "   "},
    ).get_json()
    assert patched["price_delta"] == "7.00"
    # Blank means "no group", not a group named blank.
    assert patched["exclusivity_group"] is None

    resp = client.delete(f"/api/appointment-types/{t['id']}/add-ons/{created['id']}")
    assert resp.status_code == 204
    assert client.get(f"/api/appointment-types/{t['id']}/add-ons").get_json() == []


def test_max_quantity_is_bounded(authenticated_user):
    t = _type(authenticated_user.client)
    for bad in (0, 100):
        resp = authenticated_user.client.post(
            f"/api/appointment-types/{t['id']}/add-ons",
            json={"name": "Keys", "max_quantity": bad},
        )
        assert resp.status_code == 422


def test_add_ons_are_tenant_scoped(authenticated_user, second_authenticated_client):
    t = _type(authenticated_user.client)
    a = _add_on(authenticated_user.client, t["id"])

    assert (
        second_authenticated_client.get(f"/api/appointment-types/{t['id']}/add-ons").status_code
        == 404
    )
    assert (
        second_authenticated_client.patch(
            f"/api/appointment-types/{t['id']}/add-ons/{a['id']}", json={"name": "x"}
        ).status_code
        == 404
    )


# --------------------------------------------------------------------------
# Staff appointments
# --------------------------------------------------------------------------


def test_no_add_ons_uses_the_base_price_and_duration(authenticated_user, customer):
    t = _type(authenticated_user.client)
    body = _appointment(authenticated_user, customer, t["id"]).get_json()

    assert body["price_at_booking"] == "100.00"
    assert body["end_time"].startswith("2026-12-01T10:00:00")
    assert body["applied_add_ons"] == []


def test_one_add_on_adjusts_price_and_derived_end_time(authenticated_user, customer):
    t = _type(authenticated_user.client)
    a = _add_on(authenticated_user.client, t["id"])

    resp = _appointment(authenticated_user, customer, t["id"], [{"add_on_id": a["id"]}])

    assert resp.status_code == 201, resp.get_json()
    body = resp.get_json()
    assert body["price_at_booking"] == "115.00"
    assert body["end_time"].startswith("2026-12-01T10:30:00")
    [applied] = body["applied_add_ons"]
    assert applied["add_on_id"] == a["id"]
    assert applied["quantity"] == 1
    assert applied["price_delta"] == "15.00"


def test_many_add_ons_with_quantity_and_negative_deltas(authenticated_user, customer):
    client = authenticated_user.client
    t = _type(client)
    keys = _add_on(
        client,
        t["id"],
        name="Key cut",
        price_delta="4.00",
        duration_delta_minutes=5,
        max_quantity=10,
    )
    discount = _add_on(
        client, t["id"], name="Loyalty", price_delta="-10.00", duration_delta_minutes=0
    )
    quick = _add_on(
        client, t["id"], name="Skip wash", price_delta="0.00", duration_delta_minutes=-20
    )

    body = _appointment(
        authenticated_user,
        customer,
        t["id"],
        [
            {"add_on_id": keys["id"], "quantity": 3},
            {"add_on_id": discount["id"]},
            {"add_on_id": quick["id"]},
        ],
    ).get_json()

    # 100 + 3*4 - 10 = 102; 60 + 3*5 - 20 = 55 minutes
    assert body["price_at_booking"] == "102.00"
    assert body["end_time"].startswith("2026-12-01T09:55:00")
    assert len(body["applied_add_ons"]) == 3


def test_explicit_end_time_still_overrides_the_derived_one(authenticated_user, customer):
    t = _type(authenticated_user.client)
    a = _add_on(authenticated_user.client, t["id"])

    body = _appointment(
        authenticated_user,
        customer,
        t["id"],
        [{"add_on_id": a["id"]}],
        end_time="2026-12-01T12:00:00+00:00",
    ).get_json()

    assert body["end_time"].startswith("2026-12-01T12:00:00")
    assert body["price_at_booking"] == "115.00"


def test_same_group_add_ons_are_rejected_together(authenticated_user, customer):
    client = authenticated_user.client
    t = _type(client)
    a = _add_on(client, t["id"], name="Standard", exclusivity_group="Speed")
    b = _add_on(client, t["id"], name="Rush", exclusivity_group="Speed")
    ungrouped = _add_on(client, t["id"], name="Wash")

    resp = _appointment(
        authenticated_user, customer, t["id"], [{"add_on_id": a["id"]}, {"add_on_id": b["id"]}]
    )
    assert resp.status_code == 422
    assert resp.get_json()["errors"]["reason"] == "add_on_exclusivity_conflict"

    ok = _appointment(
        authenticated_user,
        customer,
        t["id"],
        [{"add_on_id": a["id"]}, {"add_on_id": ungrouped["id"]}],
    )
    assert ok.status_code == 201


def test_quantity_above_the_add_ons_own_maximum_is_rejected(authenticated_user, customer):
    t = _type(authenticated_user.client)
    a = _add_on(authenticated_user.client, t["id"], max_quantity=2)

    resp = _appointment(
        authenticated_user, customer, t["id"], [{"add_on_id": a["id"], "quantity": 3}]
    )
    assert resp.status_code == 422


def test_add_on_from_another_type_or_hidden_is_rejected(authenticated_user, customer):
    client = authenticated_user.client
    t = _type(client)
    other = _type(client, name="Other")
    foreign = _add_on(client, other["id"])
    hidden = _add_on(client, t["id"], status="HIDDEN")

    for add_on in (foreign, hidden):
        resp = _appointment(authenticated_user, customer, t["id"], [{"add_on_id": add_on["id"]}])
        assert resp.status_code == 422


def test_add_ons_cannot_take_the_price_below_zero(authenticated_user, customer):
    t = _type(authenticated_user.client, base_price="10.00")
    a = _add_on(authenticated_user.client, t["id"], price_delta="-20.00")

    resp = _appointment(authenticated_user, customer, t["id"], [{"add_on_id": a["id"]}])
    assert resp.status_code == 422


def test_editing_the_catalogue_add_on_never_changes_a_booked_appointment(
    authenticated_user, customer
):
    client = authenticated_user.client
    t = _type(client)
    a = _add_on(client, t["id"])
    appt = _appointment(authenticated_user, customer, t["id"], [{"add_on_id": a["id"]}]).get_json()

    client.patch(
        f"/api/appointment-types/{t['id']}/add-ons/{a['id']}", json={"price_delta": "99.00"}
    )
    # An unrelated edit that re-sends the same selection keeps the snapshot.
    patched = client.patch(
        f"/api/appointments/{appt['id']}",
        json={"notes": "hi", "add_ons": [{"add_on_id": a["id"]}]},
    ).get_json()

    assert patched["price_at_booking"] == "115.00"
    assert patched["applied_add_ons"][0]["price_delta"] == "15.00"


def test_deleting_the_catalogue_add_on_keeps_the_snapshot(authenticated_user, customer):
    client = authenticated_user.client
    t = _type(client)
    a = _add_on(client, t["id"], name="Tyre check")
    appt = _appointment(authenticated_user, customer, t["id"], [{"add_on_id": a["id"]}]).get_json()

    client.delete(f"/api/appointment-types/{t['id']}/add-ons/{a['id']}")
    body = client.get(f"/api/appointments/{appt['id']}").get_json()

    [applied] = body["applied_add_ons"]
    assert applied["add_on_id"] is None
    assert applied["name"] == "Tyre check"
    assert body["price_at_booking"] == "115.00"


def test_patch_add_ons_reprices_and_shifts_end_time(authenticated_user, customer):
    client = authenticated_user.client
    t = _type(client)
    a = _add_on(client, t["id"])
    appt = _appointment(authenticated_user, customer, t["id"]).get_json()

    added = client.patch(
        f"/api/appointments/{appt['id']}", json={"add_ons": [{"add_on_id": a["id"], "quantity": 1}]}
    ).get_json()
    assert added["price_at_booking"] == "115.00"
    assert added["end_time"].startswith("2026-12-01T10:30:00")

    removed = client.patch(f"/api/appointments/{appt['id']}", json={"add_ons": []}).get_json()
    assert removed["price_at_booking"] == "100.00"
    assert removed["end_time"].startswith("2026-12-01T10:00:00")
    assert removed["applied_add_ons"] == []


def test_changing_the_type_drops_the_old_types_add_ons(authenticated_user, customer):
    client = authenticated_user.client
    t = _type(client)
    other = _type(client, name="Other", base_price="50.00")
    a = _add_on(client, t["id"])
    appt = _appointment(authenticated_user, customer, t["id"], [{"add_on_id": a["id"]}]).get_json()

    body = client.patch(
        f"/api/appointments/{appt['id']}", json={"appointment_type_id": other["id"]}
    ).get_json()

    assert body["applied_add_ons"] == []
    assert body["price_at_booking"] == "50.00"


def test_catalogue_rule_changes_never_block_editing_an_existing_booking(
    authenticated_user, customer
):
    client = authenticated_user.client
    t = _type(client)
    keys = _add_on(client, t["id"], name="Key cut", max_quantity=5)
    a = _add_on(client, t["id"], name="A")
    b = _add_on(client, t["id"], name="B")
    appt = _appointment(
        authenticated_user,
        customer,
        t["id"],
        [{"add_on_id": keys["id"], "quantity": 4}, {"add_on_id": a["id"]}, {"add_on_id": b["id"]}],
    ).get_json()

    # The owner later tightens the rules and hides one add-on.
    base = f"/api/appointment-types/{t['id']}/add-ons"
    client.patch(f"{base}/{keys['id']}", json={"max_quantity": 2})
    client.patch(f"{base}/{a['id']}", json={"exclusivity_group": "g", "status": "HIDDEN"})
    client.patch(f"{base}/{b['id']}", json={"exclusivity_group": "g"})

    # Re-sending the same selection (plus the unchanged type) still saves.
    resp = client.patch(
        f"/api/appointments/{appt['id']}",
        json={
            "appointment_type_id": t["id"],
            "notes": "ok",
            "add_ons": [
                {"add_on_id": keys["id"], "quantity": 4},
                {"add_on_id": a["id"]},
                {"add_on_id": b["id"]},
            ],
        },
    )
    assert resp.status_code == 200, resp.get_json()

    # But the new rules apply to anything newly added or increased.
    c = _add_on(client, t["id"], name="C", exclusivity_group="g")
    conflict = client.patch(
        f"/api/appointments/{appt['id']}",
        json={"add_ons": [{"add_on_id": a["id"]}, {"add_on_id": c["id"]}]},
    )
    assert conflict.status_code == 422
    too_many = client.patch(
        f"/api/appointments/{appt['id']}",
        json={"add_ons": [{"add_on_id": keys["id"], "quantity": 5}]},
    )
    assert too_many.status_code == 422


def test_resending_the_same_type_does_not_reprice_from_the_catalogue(authenticated_user, customer):
    client = authenticated_user.client
    t = _type(client)
    a = _add_on(client, t["id"])
    appt = _appointment(authenticated_user, customer, t["id"], [{"add_on_id": a["id"]}]).get_json()

    client.patch(f"/api/appointment-types/{t['id']}", json={"base_price": "250.00"})
    body = client.patch(
        f"/api/appointments/{appt['id']}",
        json={"appointment_type_id": t["id"], "notes": "just a note"},
    ).get_json()

    assert body["price_at_booking"] == "115.00"
    assert len(body["applied_add_ons"]) == 1


# --------------------------------------------------------------------------
# Public booking
# --------------------------------------------------------------------------


def _public_type(session, garage, **fields):
    values = {
        "garage_id": garage.id,
        "name": "MOT",
        "status": "ACTIVE",
        "base_price": "100.00",
        "default_duration_minutes": 60,
    }
    values.update(fields)
    t = GarageAppointmentType(**values)
    session.add(t)
    session.commit()
    return t


def _public_add_on(session, appt_type, **fields):
    from app.models.appointments.add_on import AddOn

    values = {
        "garage_id": appt_type.garage_id,
        "appointment_type_id": appt_type.id,
        "name": "Extra",
        "price_delta": "15.00",
        "duration_delta_minutes": 30,
        "max_quantity": 1,
    }
    values.update(fields)
    a = AddOn(**values)
    session.add(a)
    session.commit()
    return a


def _submit_payload(appt_type, add_ons=(), **overrides):
    payload = {
        "customer_first_name": "Alex",
        "customer_last_name": "Turner",
        "customer_email": "alex.turner@example.com",
        "customer_phone": "07123 456789",
        "vehicle_registration": "PB11 REQ",
        "preferred_date": FUTURE_DATE,
        "preferred_time": "09:30:00",
        "appointment_type_id": str(appt_type.id),
        "add_ons": list(add_ons),
    }
    payload.update(overrides)
    return payload


def test_public_payload_lists_only_active_add_ons(client, session, garage):
    t = _public_type(session, garage)
    active = _public_add_on(session, t, name="Wash")
    _public_add_on(session, t, name="Retired", status="DEPRECATED")

    body = client.get(f"/api/public/{garage.slug}").get_json()
    [public_type] = body["appointment_types"]

    assert [a["id"] for a in public_type["add_ons"]] == [str(active.id)]
    assert public_type["add_ons"][0]["price_delta"] == "15.00"


def test_public_submit_snapshots_add_on_inclusive_price_and_duration(client, session, garage):
    t = _public_type(session, garage)
    a = _public_add_on(session, t, max_quantity=5)

    resp = client.post(
        f"/api/public/{garage.slug}/booking-requests",
        json=_submit_payload(t, [{"add_on_id": str(a.id), "quantity": 2}]),
    )

    assert resp.status_code == 201, resp.get_json()
    request = BookingRequest.query.filter_by(garage_id=garage.id).one()
    assert str(request.requested_price) == "130.00"
    assert request.requested_duration_minutes == 120
    [row] = request.add_ons
    assert row.quantity == 2 and str(row.price_delta) == "15.00"


def test_public_submit_rejects_same_group_add_ons(client, session, garage):
    t = _public_type(session, garage)
    a = _public_add_on(session, t, name="A", exclusivity_group="g")
    b = _public_add_on(session, t, name="B", exclusivity_group="g")

    resp = client.post(
        f"/api/public/{garage.slug}/booking-requests",
        json=_submit_payload(t, [{"add_on_id": str(a.id)}, {"add_on_id": str(b.id)}]),
    )
    assert resp.status_code == 422


def test_availability_accounts_for_add_on_duration(client, session, garage):
    t = _public_type(session, garage)
    long_add_on = _public_add_on(session, t, duration_delta_minutes=240)
    url = f"/api/public/{garage.slug}/availability/{FUTURE_DATE}?appointment_type_id={t.id}"

    base_slots = client.get(url).get_json()["slots"]
    long_slots = client.get(f"{url}&add_ons={long_add_on.id}:1").get_json()["slots"]

    assert base_slots, "fixture garage should be open on a future weekday"
    assert len(long_slots) < len(base_slots)


def test_slot_that_only_fits_the_base_service_is_rejected_with_add_ons(client, session, garage):
    t = _public_type(session, garage)
    long_add_on = _public_add_on(session, t, duration_delta_minutes=240)
    slots = client.get(
        f"/api/public/{garage.slug}/availability/{FUTURE_DATE}?appointment_type_id={t.id}"
    ).get_json()["slots"]
    last = slots[-1]["start"]

    resp = client.post(
        f"/api/public/{garage.slug}/booking-requests",
        json=_submit_payload(t, [{"add_on_id": str(long_add_on.id)}], preferred_time=f"{last}:00"),
    )

    assert resp.status_code == 409
    assert resp.get_json()["errors"]["reason"] == "outside_hours"


def test_retried_attempt_cannot_change_its_add_ons(client, session, garage):
    t = _public_type(session, garage)
    a = _public_add_on(session, t)
    attempt = "7b1b6f46-1d7e-4b7a-9b86-3f1f1a3f0c11"
    url = f"/api/public/{garage.slug}/booking-requests"

    first = client.post(url, json=_submit_payload(t, payment_attempt_id=attempt))
    assert first.status_code == 201
    retry = client.post(
        url, json=_submit_payload(t, [{"add_on_id": str(a.id)}], payment_attempt_id=attempt)
    )
    assert retry.status_code == 409


def test_percentage_deposit_is_taken_on_the_add_on_inclusive_total(client, session, garage):
    t = _public_type(
        session, garage, deposit_required=True, deposit_type="PERCENTAGE", deposit_value="25"
    )
    a = _public_add_on(session, t, price_delta="20.00")

    resp = client.post(
        f"/api/public/{garage.slug}/booking-requests/deposit-intent",
        json=_submit_payload(t, [{"add_on_id": str(a.id)}]),
    )

    assert resp.status_code == 201, resp.get_json()
    body = resp.get_json()
    assert body["service_total"] == "120.00"
    assert body["deposit_amount"] == "30.00"
    assert body["remaining_balance"] == "90.00"


def test_fixed_deposit_is_capped_at_a_reduced_total(client, session, garage):
    t = _public_type(
        session,
        garage,
        base_price="30.00",
        deposit_required=True,
        deposit_type="FIXED",
        deposit_value="25.00",
    )
    a = _public_add_on(session, t, price_delta="-10.00")

    resp = client.post(
        f"/api/public/{garage.slug}/booking-requests/deposit-intent",
        json=_submit_payload(t, [{"add_on_id": str(a.id)}]),
    )

    assert resp.status_code == 201, resp.get_json()
    assert resp.get_json()["deposit_amount"] == "20.00"
    payment = BookingPayment.query.one()
    assert payment.amount_minor == 2000


# --------------------------------------------------------------------------
# Approval
# --------------------------------------------------------------------------


def _submitted_request(client, session, garage, add_on_fields=None):
    t = _public_type(session, garage)
    a = _public_add_on(session, t, **(add_on_fields or {}))
    resp = client.post(
        f"/api/public/{garage.slug}/booking-requests",
        json=_submit_payload(t, [{"add_on_id": str(a.id)}]),
    )
    assert resp.status_code == 201, resp.get_json()
    return t, a, BookingRequest.query.filter_by(garage_id=garage.id).one()


def test_approval_carries_the_requests_add_ons_onto_the_appointment(
    authenticated_user, session, garage
):
    _, a, request = _submitted_request(authenticated_user.client, session, garage)
    # A later catalogue edit must not change what the customer was quoted.
    a.price_delta = "99.00"
    session.commit()

    review = authenticated_user.client.get(f"/api/booking-requests/{request.id}").get_json()
    assert review["add_ons"][0]["name"] == "Extra"

    resp = authenticated_user.client.post(
        f"/api/booking-requests/{request.id}/approve",
        json={"employee_id": str(authenticated_user.user.id)},
    )

    assert resp.status_code == 200, resp.get_json()
    appointment = db.session.get(Appointment, resp.get_json()["appointment_id"])
    assert str(appointment.price_at_booking) == "115.00"
    assert (appointment.end_time - appointment.start_time) == datetime.timedelta(minutes=90)
    [row] = appointment.add_ons
    assert row.add_on_id == a.id and str(row.price_delta) == "15.00"


def test_approving_as_a_different_service_drops_the_add_ons(authenticated_user, session, garage):
    _, _, request = _submitted_request(authenticated_user.client, session, garage)
    other = _public_type(session, garage, name="Diagnostic", base_price="40.00")

    resp = authenticated_user.client.post(
        f"/api/booking-requests/{request.id}/approve",
        json={"employee_id": str(authenticated_user.user.id), "appointment_type_id": str(other.id)},
    )

    assert resp.status_code == 200, resp.get_json()
    appointment = db.session.get(Appointment, resp.get_json()["appointment_id"])
    assert appointment.add_ons == []
    assert str(appointment.price_at_booking) == "40.00"
    assert (appointment.end_time - appointment.start_time) == datetime.timedelta(minutes=60)
