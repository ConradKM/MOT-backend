"""API tests for deposit configuration on GarageAppointmentType.

POST  /api/appointment-types/       (deposit_required/deposit_type/deposit_value)
PATCH /api/appointment-types/<id>
"""

from app.models.appointments.appointment_type import GarageAppointmentType


def _create(client, **overrides):
    payload = {"name": "MOT", "base_price": "100.00"}
    payload.update(overrides)
    return client.post("/api/appointment-types/", json=payload)


def test_deposit_disabled_by_default(authenticated_user):
    resp = _create(authenticated_user.client)
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["deposit_required"] is False
    assert body["deposit_type"] is None
    assert body["deposit_value"] is None
    assert body["deposit_currency"] == "GBP"


def test_create_fixed_deposit(authenticated_user):
    resp = _create(
        authenticated_user.client,
        deposit_required=True,
        deposit_type="FIXED",
        deposit_value="20.00",
    )
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["deposit_required"] is True
    assert body["deposit_type"] == "FIXED"
    assert body["deposit_value"] == "20.00"


def test_create_percentage_deposit(authenticated_user):
    resp = _create(
        authenticated_user.client,
        deposit_required=True,
        deposit_type="PERCENTAGE",
        deposit_value="25",
    )
    assert resp.status_code == 201
    assert resp.get_json()["deposit_value"] == "25.00"


def test_fixed_deposit_cannot_exceed_service_price(authenticated_user):
    resp = _create(
        authenticated_user.client,
        base_price="50.00",
        deposit_required=True,
        deposit_type="FIXED",
        deposit_value="75.00",
    )
    assert resp.status_code == 422


def test_percentage_over_100_is_rejected(authenticated_user):
    resp = _create(
        authenticated_user.client,
        deposit_required=True,
        deposit_type="PERCENTAGE",
        deposit_value="150",
    )
    assert resp.status_code == 422


def test_negative_deposit_value_is_rejected(authenticated_user):
    resp = _create(
        authenticated_user.client,
        deposit_required=True,
        deposit_type="FIXED",
        deposit_value="-5.00",
    )
    assert resp.status_code == 422


def test_percentage_deposit_requires_a_service_price(authenticated_user):
    resp = _create(
        authenticated_user.client,
        base_price=None,
        deposit_required=True,
        deposit_type="PERCENTAGE",
        deposit_value="25",
    )
    assert resp.status_code == 422


def test_deposit_required_without_type_or_value_is_rejected(authenticated_user):
    resp = _create(authenticated_user.client, deposit_required=True)
    assert resp.status_code == 422


def test_patch_turns_on_deposit(authenticated_user, session):
    appt_type = GarageAppointmentType(
        garage_id=authenticated_user.garage.id, name="MOT", base_price="100.00"
    )
    session.add(appt_type)
    session.commit()

    resp = authenticated_user.client.patch(
        f"/api/appointment-types/{appt_type.id}",
        json={"deposit_required": True, "deposit_type": "FIXED", "deposit_value": "10.00"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["deposit_required"] is True
    assert body["deposit_value"] == "10.00"


def test_patch_turning_off_deposit_clears_type_and_value(authenticated_user, session):
    appt_type = GarageAppointmentType(
        garage_id=authenticated_user.garage.id,
        name="MOT",
        base_price="100.00",
        deposit_required=True,
        deposit_type="FIXED",
        deposit_value="10.00",
    )
    session.add(appt_type)
    session.commit()

    resp = authenticated_user.client.patch(
        f"/api/appointment-types/{appt_type.id}", json={"deposit_required": False}
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["deposit_required"] is False
    assert body["deposit_type"] is None
    assert body["deposit_value"] is None


def test_patch_lowering_base_price_below_existing_fixed_deposit_is_rejected(
    authenticated_user, session
):
    appt_type = GarageAppointmentType(
        garage_id=authenticated_user.garage.id,
        name="MOT",
        base_price="100.00",
        deposit_required=True,
        deposit_type="FIXED",
        deposit_value="80.00",
    )
    session.add(appt_type)
    session.commit()

    resp = authenticated_user.client.patch(
        f"/api/appointment-types/{appt_type.id}", json={"base_price": "50.00"}
    )
    assert resp.status_code == 422
