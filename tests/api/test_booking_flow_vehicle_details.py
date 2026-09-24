"""Vehicle details as a per-business switch - /api/booking-flow/vehicle-details
(app/booking_flow/vehicle_details.py) - and what it does to the customer
booking form, the stored booking request, and the AI voice booking tool.

The TOD case this exists for: a business with *no* booking workflow at all
turns on registration/make/model, and its customers are asked for them.
"""

import datetime
import json
import uuid

from flask_jwt_extended import create_access_token
from werkzeug.security import generate_password_hash

from app.ai_voice import tools
from app.models.booking_flow.field import BookingFlowField
from app.models.booking_flow.section import BookingFlowSection
from app.models.booking_request import BookingRequest
from app.models.employee import Employee

URL = "/api/booking-flow/vehicle-details"
ALL_ON = {
    "registration": {"enabled": True, "required": True},
    "make": {"enabled": True, "required": False},
    "model": {"enabled": True, "required": False},
}


def _future_weekday(min_days=5):
    d = datetime.datetime.now(datetime.UTC).date() + datetime.timedelta(days=min_days)
    while d.weekday() >= 5:
        d += datetime.timedelta(days=1)
    return d


def _payload(**overrides):
    base = {
        "customer_first_name": "Ada",
        "customer_last_name": "Lovelace",
        "customer_email": "ada@example.com",
        "customer_phone": "07123456789",
        "preferred_date": _future_weekday().isoformat(),
    }
    base.update(overrides)
    return base


def _bound_fields(garage_id):
    return {
        f.binds_to: f
        for f in BookingFlowField.query.filter_by(garage_id=garage_id).all()
        if f.binds_to
    }


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------


def test_off_by_default_for_a_business_with_no_workflow(authenticated_client, garage):
    body = authenticated_client.get(URL).get_json()
    assert body["fields"] == {
        "registration": {"enabled": False, "required": False},
        "make": {"enabled": False, "required": False},
        "model": {"enabled": False, "required": False},
    }


def test_owner_can_enable_registration_make_and_model(authenticated_client, garage):
    resp = authenticated_client.put(URL, json=ALL_ON)
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["fields"] == ALL_ON

    bound = _bound_fields(garage.id)
    assert set(bound) == {"ITEM_REFERENCE", "ITEM_MAKE", "ITEM_MODEL"}
    assert bound["ITEM_REFERENCE"].is_required is True
    assert bound["ITEM_MAKE"].is_required is False
    section = bound["ITEM_REFERENCE"].section
    assert section.title == "Vehicle details" and section.appointment_type_id is None


def test_saving_twice_is_idempotent(authenticated_client, garage):
    authenticated_client.put(URL, json=ALL_ON)
    authenticated_client.put(URL, json=ALL_ON)
    assert BookingFlowField.query.filter_by(garage_id=garage.id).count() == 3
    assert BookingFlowSection.query.filter_by(garage_id=garage.id).count() == 1


def test_new_vehicle_step_goes_first_and_keeps_existing_questions(
    authenticated_client, session, garage
):
    notes = BookingFlowSection(garage_id=garage.id, title="Anything else", order=0)
    session.add(notes)
    session.commit()

    authenticated_client.put(URL, json=ALL_ON)

    sections = (
        BookingFlowSection.query.filter_by(garage_id=garage.id)
        .order_by(BookingFlowSection.order)
        .all()
    )
    assert [s.title for s in sections] == ["Vehicle details", "Anything else"]


def test_reuses_the_automotive_presets_existing_fields(authenticated_client, garage):
    """Revive n Drive's shape: the preset already bound these fields - the
    switch must adopt them, not add duplicates."""
    assert (
        authenticated_client.post(
            "/api/booking-flow/presets", json={"preset": "automotive"}
        ).status_code
        == 201
    )
    before = BookingFlowField.query.filter_by(garage_id=garage.id).count()

    body = authenticated_client.get(URL).get_json()
    assert body["fields"]["registration"] == {"enabled": True, "required": True}
    assert body["fields"]["make"]["enabled"] is True

    authenticated_client.put(URL, json=ALL_ON)
    assert BookingFlowField.query.filter_by(garage_id=garage.id).count() == before


def test_make_or_model_without_registration_is_rejected(authenticated_client, garage):
    resp = authenticated_client.put(
        URL,
        json={
            "registration": {"enabled": False},
            "make": {"enabled": True},
        },
    )
    assert resp.status_code == 422
    assert "registration" in resp.get_json()["errors"]["json"]
    assert BookingFlowField.query.filter_by(garage_id=garage.id).count() == 0


def test_turning_everything_off_removes_the_step(authenticated_client, garage):
    authenticated_client.put(URL, json=ALL_ON)
    authenticated_client.put(URL, json={"model": {"enabled": False}})
    assert set(_bound_fields(garage.id)) == {"ITEM_REFERENCE", "ITEM_MAKE"}

    off = {key: {"enabled": False} for key in ALL_ON}
    body = authenticated_client.put(URL, json=off).get_json()
    assert not any(state["enabled"] for state in body["fields"].values())
    assert BookingFlowSection.query.filter_by(garage_id=garage.id).count() == 0


def test_staff_cannot_change_vehicle_details(client, session, garage, staff_role):
    staff = Employee(
        garage_id=garage.id,
        email="staff@garage-a.example",
        password_hash=generate_password_hash("Password123!"),
        roles=[staff_role],
    )
    session.add(staff)
    session.commit()
    headers = {"Authorization": f"Bearer {create_access_token(identity=str(staff.id))}"}
    assert client.get(URL, headers=headers).status_code == 200
    assert client.put(URL, json=ALL_ON, headers=headers).status_code == 403


def test_one_business_enabling_leaves_others_untouched(
    authenticated_client, second_authenticated_client, garage, second_garage
):
    authenticated_client.put(URL, json=ALL_ON)
    other = second_authenticated_client.get(URL).get_json()
    assert not any(state["enabled"] for state in other["fields"].values())
    assert BookingFlowField.query.filter_by(garage_id=second_garage.id).count() == 0


# --------------------------------------------------------------------------
# Customer booking form + stored booking
# --------------------------------------------------------------------------


def test_customer_form_renders_and_stores_enabled_vehicle_fields(
    authenticated_client, client, garage
):
    authenticated_client.put(URL, json=ALL_ON)

    flow = client.get(f"/api/public/{garage.slug}/booking-flow").get_json()
    # The public payload deliberately doesn't expose bindings - labels only.
    by_label = {f["label"]: f for s in flow["sections"] for f in s["fields"]}
    assert set(by_label) == {"Registration number", "Make", "Model"}
    fields = {
        "ITEM_REFERENCE": by_label["Registration number"],
        "ITEM_MAKE": by_label["Make"],
        "ITEM_MODEL": by_label["Model"],
    }
    assert fields["ITEM_REFERENCE"]["is_required"] is True

    # Required registration is enforced for this business...
    missing = client.post(f"/api/public/{garage.slug}/booking-requests", json=_payload())
    assert missing.status_code == 422

    resp = client.post(
        f"/api/public/{garage.slug}/booking-requests",
        json=_payload(
            answers=[
                {"field_id": fields["ITEM_REFERENCE"]["id"], "value": "TD24 ABC"},
                {"field_id": fields["ITEM_MAKE"]["id"], "value": "Tesla"},
                {"field_id": fields["ITEM_MODEL"]["id"], "value": "Model 3"},
            ]
        ),
    )
    assert resp.status_code == 201, resp.get_json()
    request = BookingRequest.query.filter_by(garage_id=garage.id).one()
    assert (request.vehicle_registration, request.vehicle_make, request.vehicle_model) == (
        "TD24 ABC",
        "Tesla",
        "Model 3",
    )

    # ...and staff see it on the booking request they review.
    staff_view = authenticated_client.get(f"/api/booking-requests/{request.id}").get_json()
    assert staff_view["vehicle_registration"] == "TD24 ABC"
    assert staff_view["vehicle_make"] == "Tesla"
    assert staff_view["vehicle_model"] == "Model 3"


def test_business_without_vehicle_fields_is_unaffected(
    authenticated_client, client, garage, second_garage
):
    authenticated_client.put(URL, json=ALL_ON)  # garage A only

    flow = client.get(f"/api/public/{second_garage.slug}/booking-flow").get_json()
    assert flow["sections"] == []
    resp = client.post(f"/api/public/{second_garage.slug}/booking-requests", json=_payload())
    assert resp.status_code == 201, resp.get_json()
    request = BookingRequest.query.filter_by(garage_id=second_garage.id).one()
    assert request.vehicle_registration is None and request.vehicle_make is None


# --------------------------------------------------------------------------
# AI voice booking follows the same configuration
# --------------------------------------------------------------------------


def _voice_types(garage):
    return json.loads(tools.dispatch_tool(garage, "", "get_appointment_types", "{}"))


def _voice_book(garage, appointment_type, day, **vehicle):
    args = {
        "appointment_type_id": str(appointment_type.id),
        "date": day.isoformat(),
        "time": "10:00",
        "first_name": "Ada",
        "last_name": "Lovelace",
        "phone": "07123456789",
        **vehicle,
    }
    return json.loads(
        tools.dispatch_tool(
            garage,
            "+447123456789",
            "create_booking",
            json.dumps(args),
            tool_call_id=f"tool_{uuid.uuid4().hex}",
            call_id=f"rtc_{uuid.uuid4().hex}",
        )
    )


def test_voice_reports_and_enforces_enabled_vehicle_details(
    authenticated_client, garage, garage_schedule, appointment_type
):
    authenticated_client.put(URL, json=ALL_ON)
    listed = _voice_types(garage)["appointment_types"][0]
    assert listed["vehicle_details"] == {
        "registration": "required",
        "make": "optional",
        "model": "optional",
    }

    day = _future_weekday()
    refused = _voice_book(garage, appointment_type, day)
    assert refused["ok"] is False and "registration" in refused["error"]

    booked = _voice_book(
        garage,
        appointment_type,
        day,
        vehicle_registration="td24 abc",
        vehicle_make="Tesla",
        vehicle_model="Model 3",
    )
    assert booked["ok"] is True, booked
    request = BookingRequest.query.filter_by(garage_id=garage.id).one()
    assert (request.vehicle_registration, request.vehicle_make, request.vehicle_model) == (
        "TD24 ABC",
        "Tesla",
        "Model 3",
    )


def test_voice_never_requires_a_registration_the_business_does_not_ask_for(
    garage, garage_schedule, appointment_type
):
    listed = _voice_types(garage)["appointment_types"][0]
    assert set(listed["vehicle_details"].values()) == {"not_asked"}

    booked = _voice_book(garage, appointment_type, _future_weekday())
    assert booked["ok"] is True, booked
    assert BookingRequest.query.filter_by(garage_id=garage.id).one().vehicle_registration is None
