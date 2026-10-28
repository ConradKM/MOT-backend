"""Bulk import at onboarding and from Platform Admin: the onboarding spec's
optional ``appointments`` (CLI and provisioning alike), the tenant
``services/bulk`` endpoint, and the wizard's stateless preview."""

import json
from copy import deepcopy

import pytest

from app.garages.business_onboarding import (
    BusinessSpecError,
    onboard_business,
    parse_business_spec,
)
from app.models.appointments.add_on import AddOn
from app.models.appointments.appointment import Appointment
from app.models.appointments.appointment_type import GarageAppointmentType
from app.models.appointments.appointment_type_group import AppointmentTypeGroup
from app.models.customer import Customer
from app.models.employee import Employee
from app.models.garage import Garage
from app.models.platform.audit_log import PlatformAuditLog
from scripts import onboard_business as cli
from tests.api.test_platform_admin_provisioning import _payload as provision_payload

TEMP_PW = "temp-pw-abc123"
TENANTS = "/api/platform-admin/tenants"
PREVIEW = "/api/platform-admin/import/preview"

# 2030-01-07 is a Monday.
SPEC = {
    "business": {"name": "Diary Garage"},
    "owner": {"email": "owner@diary.example", "first_name": "Dee"},
    "services": [{"name": "MOT", "default_duration_minutes": 45, "base_price": "54.85"}],
    "appointments": [
        {
            "service": "mot",
            "start": "2030-01-07T09:30",
            "customer": {"first_name": "Sam", "last_name": "Lee", "phone": "07123 456789"},
        },
        {
            "service": "MOT",
            "start": "2030-01-07T11:00",
            "employee_email": "OWNER@diary.example",
            "customer": {"first_name": "Alex", "last_name": "Kim"},
            "notes": "Silver Golf",
        },
    ],
}


def _appointment(**overrides):
    row = deepcopy(SPEC["appointments"][0])
    row.update(overrides)
    return row


# --------------------------------------------------------------------------
# Onboarding spec / CLI
# --------------------------------------------------------------------------


def test_a_spec_without_appointments_parses_as_before():
    raw = {key: value for key, value in SPEC.items() if key != "appointments"}
    assert parse_business_spec(raw).appointments == []


def test_spec_appointments_must_be_a_list():
    with pytest.raises(BusinessSpecError, match="appointments must be a list"):
        parse_business_spec({**SPEC, "appointments": {"service": "MOT"}})


def test_onboarding_imports_the_specs_appointments(session):
    result = onboard_business(parse_business_spec(SPEC), temp_password=TEMP_PW)

    assert result.appointments.counts == {"new": 2, "exists": 0, "error": 0, "warning": 0}
    appointments = session.query(Appointment).order_by(Appointment.start_time).all()
    assert [a.employee_id for a in appointments] == [result.owner.id] * 2
    assert all(a.imported_at is not None for a in appointments)
    assert str(appointments[0].price_at_booking) == "54.85"
    assert (appointments[0].end_time - appointments[0].start_time).seconds == 45 * 60
    assert appointments[1].notes == "Silver Golf"
    assert session.query(Customer).count() == 2


def test_onboarding_creates_the_specs_service_add_ons(session):
    services = [{**SPEC["services"][0], "add_ons": [{"name": "Retest", "price_delta": "20"}]}]
    result = onboard_business(
        parse_business_spec({**SPEC, "services": services}), temp_password=TEMP_PW
    )

    add_on = session.query(AddOn).one()
    assert (add_on.name, str(add_on.price_delta)) == ("Retest", "20.00")
    assert add_on.garage_id == result.garage.id


def test_a_bad_spec_add_on_names_its_path():
    services = [{**SPEC["services"][0], "add_ons": [{"name": "Retest", "max_quantity": 0}]}]
    with pytest.raises(BusinessSpecError, match=r"services\[0\]\.add_ons\[0\]\.max_quantity"):
        parse_business_spec({**SPEC, "services": services})


def test_an_appointment_error_fails_the_whole_onboarding(session):
    spec = parse_business_spec({**SPEC, "appointments": [_appointment(service="Tyres")]})

    with pytest.raises(BusinessSpecError, match=r"appointments\[0\]\.service"):
        onboard_business(spec, temp_password=TEMP_PW, commit=False)
    session.rollback()
    assert session.query(Garage).count() == 0


def test_appointment_warnings_can_be_refused(session):
    clash = [_appointment(), _appointment(customer={"first_name": "B", "last_name": "C"})]
    spec = parse_business_spec({**SPEC, "appointments": clash})

    with pytest.raises(BusinessSpecError, match="must be acknowledged"):
        onboard_business(spec, temp_password=TEMP_PW, allow_appointment_warnings=False)
    session.rollback()

    result = onboard_business(spec, temp_password=TEMP_PW)
    assert result.appointments.counts["warning"] == 1


def test_the_cli_onboards_a_spec_with_appointments(app, tmp_path, monkeypatch, capsys, session):
    path = tmp_path / "diary.json"
    path.write_text(json.dumps(SPEC))
    monkeypatch.setattr("app.create_app", lambda *a, **k: app)

    assert cli.main([str(path), "--dry-run"]) == 0
    assert "2 appointment(s)" in capsys.readouterr().out
    assert session.query(Garage).count() == 0

    assert cli.main([str(path)]) == 0
    assert "appointments    2 imported" in capsys.readouterr().out
    assert session.query(Appointment).count() == 2


# --------------------------------------------------------------------------
# Provisioning from Platform Admin
# --------------------------------------------------------------------------


def _with_appointments(rows, **extra):
    payload = provision_payload(owner={"email": "owner@diary.example"}, **extra)
    payload["services"][0]["name"] = "MOT"
    payload["appointments"] = rows
    return payload


def test_provisioning_imports_appointments_and_audits_counts_only(platform_client, session):
    response = platform_client.post(TENANTS, json=_with_appointments(SPEC["appointments"]))

    assert response.status_code == 201, response.get_json()
    assert response.get_json()["appointments_imported"] == {"new": 2, "exists": 0}
    assert session.query(Appointment).count() == 2
    entry = PlatformAuditLog.query.filter_by(action="tenant.create").one()
    assert entry.details["appointments_imported"] == 2
    assert "Sam" not in json.dumps(entry.details)


def test_provisioning_without_appointments_reports_none(platform_client):
    body = platform_client.post(TENANTS, json=provision_payload()).get_json()
    assert body["appointments_imported"] == {"new": 0, "exists": 0}


def test_provisioning_refuses_bad_appointments_and_creates_nothing(platform_client, session):
    rows = [_appointment(start="tomorrow")]
    response = platform_client.post(TENANTS, json=_with_appointments(rows))

    assert response.status_code == 422
    assert "appointments[0].start" in response.get_json()["message"]
    assert session.query(Garage).count() == 0


def test_provisioning_needs_warnings_acknowledged(platform_client, session):
    # Sunday: the provisioning payload's opening hours close the business.
    rows = [_appointment(start="2030-01-06T10:00")]

    refused = platform_client.post(TENANTS, json=_with_appointments(rows))
    assert refused.status_code == 422
    assert session.query(Garage).count() == 0

    accepted = platform_client.post(
        TENANTS, json=_with_appointments(rows, acknowledge_appointment_warnings=True)
    )
    assert accepted.status_code == 201


def test_provisioning_creates_groups_named_by_services(platform_client, session):
    payload = provision_payload()
    payload["services"][0]["group"] = "Testing"
    assert platform_client.post(TENANTS, json=payload).status_code == 201
    group = session.query(AppointmentTypeGroup).one()
    assert group.name == "Testing"
    assert session.query(GarageAppointmentType).filter_by(group_id=group.id).count() == 1


# --------------------------------------------------------------------------
# Tenant services/bulk
# --------------------------------------------------------------------------


@pytest.fixture()
def tenant(platform_client):
    body = platform_client.post(TENANTS, json=provision_payload()).get_json()
    return body["garage"]["id"]


def _bulk(client, tenant_id, payload, **flags):
    return client.post(f"{TENANTS}/{tenant_id}/services/bulk", json={"payload": payload, **flags})


def test_tenant_bulk_services_previews_then_commits(platform_client, tenant, session):
    payload = {"services": [{"name": "mot TEST"}, {"name": "Tyres", "base_price": "40"}]}

    preview = _bulk(platform_client, tenant, payload).get_json()
    assert [i["status"] for i in preview["items"]] == ["EXISTS", "NEW"]
    assert session.query(GarageAppointmentType).count() == 2

    response = _bulk(platform_client, tenant, payload, dry_run=False)
    assert response.status_code == 200
    assert session.query(GarageAppointmentType).count() == 3
    entry = PlatformAuditLog.query.filter_by(action="tenant.services.bulk_import").one()
    assert entry.details == {"new": 1, "exists": 1}


def test_tenant_bulk_services_preview_is_not_audited(platform_client, tenant):
    _bulk(platform_client, tenant, {"services": [{"name": "Tyres"}]})
    assert PlatformAuditLog.query.filter_by(action="tenant.services.bulk_import").count() == 0


def test_tenant_bulk_services_is_superadmin_only(support_client, tenant):
    assert _bulk(support_client, tenant, {"services": []}).status_code == 403


def test_tenant_bulk_services_refuses_a_garage_token(authenticated_client, tenant):
    response = _bulk(authenticated_client, tenant, {"services": []})
    assert response.status_code in (401, 403)


def test_tenant_bulk_services_for_an_unknown_business_is_404(platform_client):
    unknown = "00000000-0000-0000-0000-000000000000"
    assert _bulk(platform_client, unknown, {"services": []}).status_code == 404


def test_platform_admin_formats_match_the_owner_apps(platform_client, authenticated_client):
    admin = platform_client.get("/api/platform-admin/import/formats").get_json()
    owner = authenticated_client.get("/api/bulk-import/formats").get_json()
    assert admin == owner


# --------------------------------------------------------------------------
# Stateless preview for the onboarding wizard
# --------------------------------------------------------------------------


def _context(**overrides):
    return {
        "owner": {"email": "owner@diary.example"},
        "services": [{"name": "MOT", "default_duration_minutes": 45, "base_price": "54.85"}],
        **overrides,
    }


def test_preview_checks_services_against_the_draft(platform_client, session):
    response = platform_client.post(
        PREVIEW,
        json={
            "kind": "services",
            "payload": {"services": [{"name": "mot"}, {"name": "Tyres"}]},
            "context": _context(),
        },
    )

    assert response.status_code == 200
    assert [i["status"] for i in response.get_json()["items"]] == ["EXISTS", "NEW"]
    assert session.query(Garage).count() == 0


def test_preview_checks_appointments_against_the_draft_and_writes_nothing(platform_client, session):
    response = platform_client.post(
        PREVIEW,
        json={
            "kind": "appointments",
            "payload": {"appointments": SPEC["appointments"] + [_appointment(service="Nope")]},
            "context": _context(),
        },
    )

    body = response.get_json()
    assert response.status_code == 200
    assert [i["status"] for i in body["items"]] == ["NEW", "NEW", "ERROR"]
    assert body["items"][1]["normalised"]["employee"]["email"] == "owner@diary.example"
    assert body["dry_run"] is True and body["committed"] is False
    assert body["context"] == {"timezone": "Europe/London"}
    for model in (Garage, Employee, Customer, Appointment, PlatformAuditLog):
        assert session.query(model).count() == 0, model


def test_preview_flags_out_of_hours_from_the_draft_hours(platform_client):
    hours = [{"weekday": 0, "opens_at": "12:00", "closes_at": "18:00"}]
    body = platform_client.post(
        PREVIEW,
        json={
            "kind": "appointments",
            "payload": {"appointments": [_appointment()]},
            "context": _context(opening_hours=hours),
        },
    ).get_json()
    assert "out_of_hours" in body["items"][0]["normalised"]["flags"]


def test_preview_matches_what_provisioning_then_does(platform_client):
    rows = SPEC["appointments"]
    payload = _with_appointments(rows)
    context = {
        key: payload[key] for key in ("owner", "services", "opening_hours", "booking_settings")
    }
    preview = platform_client.post(
        PREVIEW,
        json={"kind": "appointments", "payload": {"appointments": rows}, "context": context},
    ).get_json()

    created = platform_client.post(TENANTS, json=payload).get_json()
    assert created["appointments_imported"] == {
        "new": preview["counts"]["new"],
        "exists": preview["counts"]["exists"],
    }


def test_preview_works_before_the_owner_is_known(platform_client):
    body = platform_client.post(
        PREVIEW,
        json={
            "kind": "appointments",
            "payload": {"appointments": [_appointment()]},
            "context": {"services": _context()["services"]},
        },
    ).get_json()
    assert body["items"][0]["status"] == "NEW"


def test_preview_with_an_invalid_draft_is_a_422(platform_client):
    duplicate = [{"name": "MOT"}, {"name": "mot"}]
    response = platform_client.post(
        PREVIEW,
        json={"kind": "services", "payload": {"services": []}, "context": {"services": duplicate}},
    )
    assert response.status_code == 422
    assert "draft business" in response.get_json()["message"]


def test_preview_with_a_bad_payload_is_a_422(platform_client):
    response = platform_client.post(
        PREVIEW, json={"kind": "services", "payload": [], "context": {}}
    )
    assert response.status_code == 422


def test_preview_is_superadmin_only(support_client):
    response = support_client.post(PREVIEW, json={"kind": "services", "payload": {"services": []}})
    assert response.status_code == 403
