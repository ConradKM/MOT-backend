"""API tests for the checklist template nested under an appointment type."""

import uuid


def _create_type(client, name="MOT"):
    return client.post("/api/appointment-types/", json={"name": name}).get_json()


# --------------------------------------------------------------------------
# Success cases
# --------------------------------------------------------------------------


def test_owner_can_create_checklist_template(authenticated_user):
    appt_type = _create_type(authenticated_user.client)

    resp = authenticated_user.client.post(f"/api/appointment-types/{appt_type['id']}/checklist-template")

    assert resp.status_code == 201
    body = resp.get_json()
    assert body["appointment_type_id"] == appt_type["id"]
    assert body["items"] == []


def test_creating_a_second_template_for_the_same_type_conflicts(authenticated_user):
    appt_type = _create_type(authenticated_user.client)
    authenticated_user.client.post(f"/api/appointment-types/{appt_type['id']}/checklist-template")

    resp = authenticated_user.client.post(f"/api/appointment-types/{appt_type['id']}/checklist-template")
    assert resp.status_code == 409


def test_get_checklist_template_before_creation_returns_404(authenticated_user):
    appt_type = _create_type(authenticated_user.client)

    resp = authenticated_user.client.get(f"/api/appointment-types/{appt_type['id']}/checklist-template")
    assert resp.status_code == 404


def test_owner_can_add_checklist_template_item(authenticated_user):
    appt_type = _create_type(authenticated_user.client)
    authenticated_user.client.post(f"/api/appointment-types/{appt_type['id']}/checklist-template")

    resp = authenticated_user.client.post(
        f"/api/appointment-types/{appt_type['id']}/checklist-template/items",
        json={
            "label": "Check front brake pad thickness",
            "order": 0,
            "is_compulsory": True,
            "media_type": "PHOTO",
            "media_required_for_statuses": ["MAJOR", "DANGEROUS"],
        },
    )

    assert resp.status_code == 201
    body = resp.get_json()
    assert body["label"] == "Check front brake pad thickness"
    assert body["is_compulsory"] is True
    assert body["media_type"] == "PHOTO"
    assert body["media_required_for_statuses"] == ["MAJOR", "DANGEROUS"]


def test_checklist_template_item_defaults(authenticated_user):
    appt_type = _create_type(authenticated_user.client)
    authenticated_user.client.post(f"/api/appointment-types/{appt_type['id']}/checklist-template")

    resp = authenticated_user.client.post(
        f"/api/appointment-types/{appt_type['id']}/checklist-template/items",
        json={"label": "Check tyre tread depth"},
    )

    assert resp.status_code == 201
    body = resp.get_json()
    assert body["order"] == 0
    assert body["is_compulsory"] is False
    assert body["media_type"] == "NONE"
    assert body["media_required_for_statuses"] == []


def test_list_and_get_checklist_template_items(authenticated_user):
    appt_type = _create_type(authenticated_user.client)
    authenticated_user.client.post(f"/api/appointment-types/{appt_type['id']}/checklist-template")
    created = authenticated_user.client.post(
        f"/api/appointment-types/{appt_type['id']}/checklist-template/items",
        json={"label": "Check tyre tread depth"},
    ).get_json()

    list_resp = authenticated_user.client.get(
        f"/api/appointment-types/{appt_type['id']}/checklist-template/items"
    )
    assert list_resp.status_code == 200
    assert any(i["id"] == created["id"] for i in list_resp.get_json())

    get_resp = authenticated_user.client.get(
        f"/api/appointment-types/{appt_type['id']}/checklist-template/items/{created['id']}"
    )
    assert get_resp.status_code == 200
    assert get_resp.get_json()["id"] == created["id"]


def test_owner_can_update_checklist_template_item(authenticated_user):
    appt_type = _create_type(authenticated_user.client)
    authenticated_user.client.post(f"/api/appointment-types/{appt_type['id']}/checklist-template")
    created = authenticated_user.client.post(
        f"/api/appointment-types/{appt_type['id']}/checklist-template/items",
        json={"label": "Check tyre tread depth"},
    ).get_json()

    resp = authenticated_user.client.patch(
        f"/api/appointment-types/{appt_type['id']}/checklist-template/items/{created['id']}",
        json={"is_compulsory": True},
    )

    assert resp.status_code == 200
    assert resp.get_json()["is_compulsory"] is True


def test_owner_can_delete_checklist_template_item(authenticated_user):
    appt_type = _create_type(authenticated_user.client)
    authenticated_user.client.post(f"/api/appointment-types/{appt_type['id']}/checklist-template")
    created = authenticated_user.client.post(
        f"/api/appointment-types/{appt_type['id']}/checklist-template/items",
        json={"label": "Check tyre tread depth"},
    ).get_json()

    resp = authenticated_user.client.delete(
        f"/api/appointment-types/{appt_type['id']}/checklist-template/items/{created['id']}"
    )
    assert resp.status_code == 204

    get_resp = authenticated_user.client.get(
        f"/api/appointment-types/{appt_type['id']}/checklist-template/items/{created['id']}"
    )
    assert get_resp.status_code == 404


def test_owner_can_delete_checklist_template(authenticated_user):
    appt_type = _create_type(authenticated_user.client)
    authenticated_user.client.post(f"/api/appointment-types/{appt_type['id']}/checklist-template")

    resp = authenticated_user.client.delete(f"/api/appointment-types/{appt_type['id']}/checklist-template")
    assert resp.status_code == 204

    get_resp = authenticated_user.client.get(f"/api/appointment-types/{appt_type['id']}/checklist-template")
    assert get_resp.status_code == 404


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def test_add_item_missing_label(authenticated_user):
    appt_type = _create_type(authenticated_user.client)
    authenticated_user.client.post(f"/api/appointment-types/{appt_type['id']}/checklist-template")

    resp = authenticated_user.client.post(
        f"/api/appointment-types/{appt_type['id']}/checklist-template/items", json={}
    )
    assert resp.status_code == 422
    assert "label" in resp.get_json()["errors"]["json"]


def test_add_item_invalid_media_type(authenticated_user):
    appt_type = _create_type(authenticated_user.client)
    authenticated_user.client.post(f"/api/appointment-types/{appt_type['id']}/checklist-template")

    resp = authenticated_user.client.post(
        f"/api/appointment-types/{appt_type['id']}/checklist-template/items",
        json={"label": "X", "media_type": "AUDIO"},
    )
    assert resp.status_code == 422


def test_add_item_invalid_media_required_for_statuses(authenticated_user):
    appt_type = _create_type(authenticated_user.client)
    authenticated_user.client.post(f"/api/appointment-types/{appt_type['id']}/checklist-template")

    resp = authenticated_user.client.post(
        f"/api/appointment-types/{appt_type['id']}/checklist-template/items",
        json={"label": "X", "media_required_for_statuses": ["NOT_A_REAL_STATUS"]},
    )
    assert resp.status_code == 422


def test_create_template_for_nonexistent_appointment_type_returns_404(authenticated_user):
    resp = authenticated_user.client.post(f"/api/appointment-types/{uuid.uuid4()}/checklist-template")
    assert resp.status_code == 404


# --------------------------------------------------------------------------
# Authorization
# --------------------------------------------------------------------------


def test_create_template_requires_auth(client):
    resp = client.post(f"/api/appointment-types/{uuid.uuid4()}/checklist-template")
    assert resp.status_code == 401


def test_staff_cannot_create_checklist_template(authenticated_user, session):
    appt_type = _create_type(authenticated_user.client)

    authenticated_user.user.roles = []
    session.commit()

    resp = authenticated_user.client.post(f"/api/appointment-types/{appt_type['id']}/checklist-template")
    assert resp.status_code == 403


def test_staff_cannot_add_checklist_template_item(authenticated_user, session):
    appt_type = _create_type(authenticated_user.client)
    authenticated_user.client.post(f"/api/appointment-types/{appt_type['id']}/checklist-template")

    authenticated_user.user.roles = []
    session.commit()

    resp = authenticated_user.client.post(
        f"/api/appointment-types/{appt_type['id']}/checklist-template/items",
        json={"label": "X"},
    )
    assert resp.status_code == 403


# --------------------------------------------------------------------------
# Multi-tenancy
# --------------------------------------------------------------------------


def test_user_a_cannot_see_garage_bs_checklist_template(
    authenticated_user, second_authenticated_client
):
    b_type = _create_type(second_authenticated_client, "Garage B Type")
    second_authenticated_client.post(f"/api/appointment-types/{b_type['id']}/checklist-template")

    resp = authenticated_user.client.get(f"/api/appointment-types/{b_type['id']}/checklist-template")
    assert resp.status_code == 404
