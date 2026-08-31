"""API tests for the per-appointment checklist instance.

Covers snapshot-on-first-use semantics: once created, a checklist instance
must not change when its template is edited afterwards.
"""

import uuid


def _make_appointment_with_template(authenticated_user, customer, template_items=None):
    appt_type = authenticated_user.client.post(
        "/api/appointment-types/", json={"name": "MOT"}
    ).get_json()
    authenticated_user.client.post(f"/api/appointment-types/{appt_type['id']}/checklist-template")

    for item in template_items or [{"label": "Check tyre tread depth", "is_compulsory": True}]:
        authenticated_user.client.post(
            f"/api/appointment-types/{appt_type['id']}/checklist-template/items", json=item
        )

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

    return appt_type, appointment


# --------------------------------------------------------------------------
# Success cases
# --------------------------------------------------------------------------


def test_snapshot_checklist_for_appointment(authenticated_user, customer):
    _, appointment = _make_appointment_with_template(authenticated_user, customer)

    resp = authenticated_user.client.post(f"/api/appointments/{appointment['id']}/checklist")

    assert resp.status_code == 201
    body = resp.get_json()
    assert body["appointment_id"] == appointment["id"]
    assert len(body["items"]) == 1
    assert body["items"][0]["label"] == "Check tyre tread depth"
    assert body["items"][0]["status"] == "NOT_CHECKED"


def test_get_checklist_after_snapshot(authenticated_user, customer):
    _, appointment = _make_appointment_with_template(authenticated_user, customer)
    authenticated_user.client.post(f"/api/appointments/{appointment['id']}/checklist")

    resp = authenticated_user.client.get(f"/api/appointments/{appointment['id']}/checklist")

    assert resp.status_code == 200
    assert len(resp.get_json()["items"]) == 1


def test_get_checklist_before_snapshot_returns_404(authenticated_user, customer):
    _, appointment = _make_appointment_with_template(authenticated_user, customer)

    resp = authenticated_user.client.get(f"/api/appointments/{appointment['id']}/checklist")
    assert resp.status_code == 404


def test_snapshotting_twice_conflicts(authenticated_user, customer):
    _, appointment = _make_appointment_with_template(authenticated_user, customer)
    authenticated_user.client.post(f"/api/appointments/{appointment['id']}/checklist")

    resp = authenticated_user.client.post(f"/api/appointments/{appointment['id']}/checklist")
    assert resp.status_code == 409


def test_snapshot_without_a_template_fails(authenticated_user, customer):
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

    resp = authenticated_user.client.post(f"/api/appointments/{appointment['id']}/checklist")
    assert resp.status_code == 422


def test_logging_an_item_result_sets_status_and_notes(authenticated_user, customer):
    _, appointment = _make_appointment_with_template(authenticated_user, customer)
    checklist = authenticated_user.client.post(
        f"/api/appointments/{appointment['id']}/checklist"
    ).get_json()
    item_id = checklist["items"][0]["id"]

    resp = authenticated_user.client.patch(
        f"/api/appointment-checklists/{checklist['id']}/items/{item_id}",
        json={"status": "MAJOR", "notes": "Tread at 1mm, needs replacing"},
    )

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "MAJOR"
    assert body["notes"] == "Tread at 1mm, needs replacing"


def test_logging_a_result_records_who_and_when(authenticated_user, customer):
    _, appointment = _make_appointment_with_template(authenticated_user, customer)
    checklist = authenticated_user.client.post(
        f"/api/appointments/{appointment['id']}/checklist"
    ).get_json()
    item_id = checklist["items"][0]["id"]

    resp = authenticated_user.client.patch(
        f"/api/appointment-checklists/{checklist['id']}/items/{item_id}",
        json={"status": "PASS"},
    )

    body = resp.get_json()
    assert body["completed_by_employee_id"] == str(authenticated_user.user.id)
    assert body["completed_at"] is not None


def test_client_cannot_set_completed_by_or_completed_at_directly(authenticated_user, customer):
    _, appointment = _make_appointment_with_template(authenticated_user, customer)
    checklist = authenticated_user.client.post(
        f"/api/appointments/{appointment['id']}/checklist"
    ).get_json()
    item_id = checklist["items"][0]["id"]

    # Neither field is on AppointmentChecklistItemUpdateSchema, and unknown
    # fields are rejected outright (matching the same convention already
    # established for e.g. PATCH /api/garage) - not silently dropped.
    resp = authenticated_user.client.patch(
        f"/api/appointment-checklists/{checklist['id']}/items/{item_id}",
        json={
            "status": "PASS",
            "completed_by_employee_id": str(uuid.uuid4()),
            "completed_at": "2020-01-01T00:00:00+00:00",
        },
    )

    assert resp.status_code == 422
    errors = resp.get_json()["errors"]["json"]
    assert "completed_by_employee_id" in errors
    assert "completed_at" in errors

    # The legitimate way (status only) still records who/when correctly.
    legit = authenticated_user.client.patch(
        f"/api/appointment-checklists/{checklist['id']}/items/{item_id}",
        json={"status": "PASS"},
    )
    assert legit.status_code == 200
    assert legit.get_json()["completed_by_employee_id"] == str(authenticated_user.user.id)


# --------------------------------------------------------------------------
# Snapshot isolation - the central guarantee of this feature
# --------------------------------------------------------------------------


def test_editing_template_after_snapshot_does_not_change_existing_checklist(
    authenticated_user, customer
):
    appt_type, appointment = _make_appointment_with_template(authenticated_user, customer)
    authenticated_user.client.post(f"/api/appointments/{appointment['id']}/checklist")

    # Add a brand-new item to the template after the checklist was snapshotted.
    authenticated_user.client.post(
        f"/api/appointment-types/{appt_type['id']}/checklist-template/items",
        json={"label": "Newly added step", "order": 1},
    )

    refetched = authenticated_user.client.get(
        f"/api/appointments/{appointment['id']}/checklist"
    ).get_json()

    assert len(refetched["items"]) == 1
    assert refetched["items"][0]["label"] == "Check tyre tread depth"


def test_editing_template_item_after_snapshot_does_not_change_logged_item(
    authenticated_user, customer
):
    appt_type, appointment = _make_appointment_with_template(authenticated_user, customer)
    template = authenticated_user.client.get(
        f"/api/appointment-types/{appt_type['id']}/checklist-template"
    ).get_json()
    template_item_id = template["items"][0]["id"]

    checklist = authenticated_user.client.post(
        f"/api/appointments/{appointment['id']}/checklist"
    ).get_json()

    # Edit the original template item's label after the snapshot was taken.
    authenticated_user.client.patch(
        f"/api/appointment-types/{appt_type['id']}/checklist-template/items/{template_item_id}",
        json={"label": "Renamed after the fact"},
    )

    refetched = authenticated_user.client.get(
        f"/api/appointments/{appointment['id']}/checklist"
    ).get_json()

    assert refetched["items"][0]["label"] == "Check tyre tread depth"
    assert refetched["id"] == checklist["id"]


def test_deleting_template_after_snapshot_preserves_the_checklist_instance(
    authenticated_user, customer
):
    appt_type, appointment = _make_appointment_with_template(authenticated_user, customer)
    authenticated_user.client.post(f"/api/appointments/{appointment['id']}/checklist")

    resp = authenticated_user.client.delete(f"/api/appointment-types/{appt_type['id']}/checklist-template")
    assert resp.status_code == 204

    refetched = authenticated_user.client.get(f"/api/appointments/{appointment['id']}/checklist")
    assert refetched.status_code == 200
    assert len(refetched.get_json()["items"]) == 1


# --------------------------------------------------------------------------
# Validation / authorization
# --------------------------------------------------------------------------


def test_log_result_invalid_status(authenticated_user, customer):
    _, appointment = _make_appointment_with_template(authenticated_user, customer)
    checklist = authenticated_user.client.post(
        f"/api/appointments/{appointment['id']}/checklist"
    ).get_json()
    item_id = checklist["items"][0]["id"]

    resp = authenticated_user.client.patch(
        f"/api/appointment-checklists/{checklist['id']}/items/{item_id}",
        json={"status": "NOT_A_REAL_STATUS"},
    )
    assert resp.status_code == 422


def test_log_result_nonexistent_item_returns_404(authenticated_user, customer):
    _, appointment = _make_appointment_with_template(authenticated_user, customer)
    checklist = authenticated_user.client.post(
        f"/api/appointments/{appointment['id']}/checklist"
    ).get_json()

    resp = authenticated_user.client.patch(
        f"/api/appointment-checklists/{checklist['id']}/items/{uuid.uuid4()}",
        json={"status": "PASS"},
    )
    assert resp.status_code == 404


def test_snapshot_checklist_requires_auth(client):
    resp = client.post(f"/api/appointments/{uuid.uuid4()}/checklist")
    assert resp.status_code == 401


def test_snapshot_checklist_for_nonexistent_appointment_returns_404(authenticated_user):
    resp = authenticated_user.client.post(f"/api/appointments/{uuid.uuid4()}/checklist")
    assert resp.status_code == 404


# --------------------------------------------------------------------------
# Multi-tenancy
# --------------------------------------------------------------------------


def test_user_a_cannot_retrieve_garage_bs_appointment_checklist(
    authenticated_user, second_authenticated_client, second_garage, session
):
    from werkzeug.security import generate_password_hash

    from app.models.customer import Customer
    from app.models.employee import Employee

    b_employee = Employee(
        garage_id=second_garage.id,
        email="b-tech@garage-b.example",
        password_hash=generate_password_hash("password123"),
        role="OWNER",
    )
    b_customer = Customer(garage_id=second_garage.id, first_name="Bob", last_name="B")
    session.add_all([b_employee, b_customer])
    session.commit()

    b_type = second_authenticated_client.post(
        "/api/appointment-types/", json={"name": "MOT"}
    ).get_json()
    second_authenticated_client.post(f"/api/appointment-types/{b_type['id']}/checklist-template")
    second_authenticated_client.post(
        f"/api/appointment-types/{b_type['id']}/checklist-template/items",
        json={"label": "Garage B step"},
    )
    b_appointment = second_authenticated_client.post(
        "/api/appointments/",
        json={
            "employee_id": str(b_employee.id),
            "customer_id": str(b_customer.id),
            "start_time": "2026-09-15T09:00:00+01:00",
            "end_time": "2026-09-15T10:00:00+01:00",
            "appointment_type_id": b_type["id"],
        },
    ).get_json()
    second_authenticated_client.post(f"/api/appointments/{b_appointment['id']}/checklist")

    resp = authenticated_user.client.get(f"/api/appointments/{b_appointment['id']}/checklist")
    assert resp.status_code == 404
