"""Onboarding a business from Platform Admin.

Covers the endpoint that replaces ``scripts/onboard_business.py`` for normal
onboarding: what one request creates, what it refuses, that a password is never
chosen or returned by anyone, that the tenant it produces is immediately real
to the *public* API (it is bookable, on the hours that were set), and that the
configuration can be corrected afterwards without dropping back to the CLI.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.extensions import db
from app.models.appointments.appointment_type import GarageAppointmentType
from app.models.employee import Employee
from app.models.garage import Garage
from app.models.garage_schedule import GarageOpeningHours, GarageScheduleSettings
from app.models.password_reset_token import PasswordResetToken
from app.models.platform.audit_log import PlatformAuditLog

TENANTS = "/api/platform-admin/tenants"


def _payload(**overrides):
    payload = {
        "business": {
            "name": "Revive N Drive",
            "email": "hello@revive.example",
            "phone": "+44 20 7946 0111",
            "address": "12 Bevan Road, London",
            "postcode": "SE2 0EQ",
            "website": "https://revive.example",
            "plan": "STANDARD",
            "status": "ACTIVE",
            "internal_notes": "Referred by MazTrad.",
        },
        "owner": {
            "email": "owner@revive.example",
            "first_name": "Ada",
            "last_name": "Byron",
        },
        "services": [
            {
                "name": "MOT test",
                "description": "Class 4 MOT",
                "base_price": "54.85",
                "default_duration_minutes": 45,
                "status": "ACTIVE",
            },
            {"name": "Full service", "base_price": "180.00", "default_duration_minutes": 120},
        ],
        "opening_hours": [
            {"weekday": 0, "opens_at": "08:00", "closes_at": "18:00"},
            {"weekday": 1, "opens_at": "08:00", "closes_at": "18:00"},
            {"weekday": 2, "opens_at": "08:00", "closes_at": "18:00"},
            {"weekday": 3, "opens_at": "08:00", "closes_at": "18:00"},
            {"weekday": 4, "opens_at": "08:00", "closes_at": "17:00"},
            {"weekday": 5, "opens_at": "09:00", "closes_at": "13:00"},
            {"weekday": 6, "is_closed": True},
        ],
        "booking_settings": {
            "slot_interval_minutes": 30,
            "default_appointment_minutes": 60,
            "min_lead_time_hours": 4,
            "max_advance_days": 45,
            "capacity_per_slot": 2,
        },
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(payload.get(key), dict):
            payload[key] = {**payload[key], **value}
        else:
            payload[key] = value
    return payload


@pytest.fixture()
def onboarded(platform_client):
    response = platform_client.post(TENANTS, json=_payload())
    assert response.status_code == 201, response.get_json()
    return response.get_json()


# --------------------------------------------------------------------------
# What one request creates
# --------------------------------------------------------------------------


def test_onboarding_creates_the_whole_tenant_in_one_request(onboarded):
    garage = onboarded["garage"]

    assert onboarded["created"] is True
    assert garage["name"] == "Revive N Drive"
    assert garage["plan"] == "STANDARD"
    assert garage["status"] == "ACTIVE"
    # The slug is generated, never supplied - name stem plus a random suffix.
    assert garage["slug"].startswith("revive-n-drive-")
    assert garage["slug"] != "revive-n-drive"
    assert onboarded["public_booking_url"].endswith(f"/book/{garage['id']}")

    assert onboarded["owner"]["email"] == "owner@revive.example"
    assert {service["name"] for service in onboarded["services"]} == {"MOT test", "Full service"}
    assert onboarded["booking_settings"]["min_lead_time_hours"] == 4
    assert onboarded["booking_settings"]["max_advance_days"] == 45

    saturday = next(day for day in onboarded["opening_hours"] if day["weekday"] == 5)
    sunday = next(day for day in onboarded["opening_hours"] if day["weekday"] == 6)
    assert saturday["opens_at"].startswith("09:00")
    assert sunday["is_closed"] is True


def test_the_first_employee_is_an_owner(app, onboarded):
    owner = db.session.get(Employee, onboarded["owner"]["id"])
    assert [role.name for role in owner.roles] == ["OWNER"]
    assert str(owner.garage_id) == onboarded["garage"]["id"]


def test_internal_notes_reach_the_tenant_record(app, onboarded):
    garage = db.session.get(Garage, onboarded["garage"]["id"])
    assert garage.internal_notes == "Referred by MazTrad."


def test_onboarding_seeds_the_statuses_and_reminder_settings_a_tenant_needs(app, onboarded):
    garage = db.session.get(Garage, onboarded["garage"]["id"])
    assert garage.mot_reminder_settings is not None
    assert {role.name for role in garage.roles} == {"OWNER", "STAFF"}


# --------------------------------------------------------------------------
# The owner never has a password chosen for them
# --------------------------------------------------------------------------


def test_no_password_is_returned_anywhere_in_the_response(onboarded):
    """A credential must never reach the console, not even to be shown once."""
    for key in ("password", "temp_password", "password_hash", "token"):
        assert key not in onboarded
        assert key not in onboarded["owner"]
    assert "reset-password?token=" not in str(onboarded)


def test_a_password_supplied_by_a_client_is_refused(platform_client):
    """There is no way to *choose* an owner's password through this API."""
    payload = _payload(
        owner={"email": "second@revive.example", "password": "hunter2hunter2"},
        business={"name": "Second Garage"},
    )
    response = platform_client.post(TENANTS, json=payload)
    assert response.status_code == 422
    assert Employee.query.filter_by(email="second@revive.example").first() is None


def test_the_owner_gets_a_set_password_invite(app, onboarded):
    assert onboarded["invite_sent"] is True
    assert onboarded["owner_invite"]["state"] == "sent"

    token = PasswordResetToken.query.filter_by(
        employee_id=onboarded["owner"]["id"], used_at=None
    ).one()
    # Days, not the 30 minutes a self-service reset assumes.
    assert token.expires_at > datetime.now(UTC) + timedelta(days=1)


def test_the_owner_activates_the_account_and_can_then_sign_in(app, client, onboarded):
    """The full activation path, through the garage app's own endpoints."""
    from app.auth.reset import _hash_token

    # The raw token only ever exists in the emailed link, so re-issue one the
    # way "resend invite" does and follow that.
    resend = client.post(
        f"{TENANTS}/{onboarded['garage']['id']}/owner-invite",
        headers={"Authorization": _superadmin_header(app)},
    )
    assert resend.status_code == 200

    raw = _capture_invite_token(app, onboarded["owner"]["id"])
    assert _hash_token(raw) is not None

    reset = client.post(
        "/api/auth/reset-password",
        json={"token": raw, "password": "Sunflower-92-Bridge"},
    )
    assert reset.status_code == 200, reset.get_json()

    login = client.post(
        "/api/auth/login",
        json={"email": "owner@revive.example", "password": "Sunflower-92-Bridge"},
    )
    assert login.status_code == 200, login.get_json()
    assert login.get_json()["access_token"]


def _superadmin_header(app):
    from flask_jwt_extended import create_access_token

    from app.models.platform.admin import PlatformAdmin

    admin = PlatformAdmin.query.first()
    token = create_access_token(
        identity=str(admin.id),
        additional_claims={"account_type": "platform_admin", "role": admin.role},
    )
    return f"Bearer {token}"


def _capture_invite_token(app, owner_id):
    """The raw invite token, by re-issuing one directly.

    The API deliberately never returns it - it exists only in the email - so a
    test that needs to *follow* the link mints its own through the same
    service the endpoint uses.
    """
    from app.auth.reset import issue_reset_token

    owner = db.session.get(Employee, owner_id)
    raw = issue_reset_token(owner, minutes=60)
    db.session.commit()
    return raw


def test_invite_state_becomes_accepted_once_used(app, client, platform_client, onboarded):
    raw = _capture_invite_token(app, onboarded["owner"]["id"])
    client.post("/api/auth/reset-password", json={"token": raw, "password": "Sunflower-92-Bridge"})

    config = platform_client.get(f"{TENANTS}/{onboarded['garage']['id']}/configuration").get_json()
    assert config["owner_invite"]["state"] == "accepted"
    assert config["owner_invite"]["accepted_at"] is not None


# --------------------------------------------------------------------------
# Refusals - validated server-side, whatever the console sent
# --------------------------------------------------------------------------


def test_a_second_submission_of_the_same_owner_is_rejected(platform_client, onboarded):
    """Duplicate-submission protection: the unique constraint on
    ``employees.email``, not a client-side guard."""
    response = platform_client.post(TENANTS, json=_payload(business={"name": "Different Name"}))
    assert response.status_code == 409
    assert "already exists" in response.get_json()["message"]
    assert Garage.query.filter_by(name="Different Name").first() is None


def test_a_failed_creation_leaves_no_half_built_tenant(platform_client, onboarded):
    before = Garage.query.count()
    response = platform_client.post(
        TENANTS,
        json=_payload(business={"name": "Rolled Back Motors"}),
    )
    assert response.status_code == 409
    assert Garage.query.count() == before
    assert Garage.query.filter_by(name="Rolled Back Motors").first() is None


def test_trial_requires_a_future_end_date(platform_client):
    response = platform_client.post(
        TENANTS,
        json=_payload(business={"status": "TRIAL", "trial_ends_at": None}),
    )
    assert response.status_code == 422
    assert "trial_ends_at" in response.get_json()["message"]


def test_a_trial_end_date_in_the_past_is_rejected(platform_client):
    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    response = platform_client.post(
        TENANTS,
        json=_payload(business={"status": "TRIAL", "trial_ends_at": past}),
    )
    assert response.status_code == 422
    assert "future" in response.get_json()["message"]


def test_a_trial_end_date_without_a_trial_is_rejected(platform_client):
    future = (datetime.now(UTC) + timedelta(days=14)).isoformat()
    response = platform_client.post(
        TENANTS,
        json=_payload(business={"status": "ACTIVE", "trial_ends_at": future}),
    )
    assert response.status_code == 422


def test_a_trial_is_created_with_its_expiry(platform_client):
    future = (datetime.now(UTC) + timedelta(days=14)).replace(microsecond=0)
    response = platform_client.post(
        TENANTS,
        json=_payload(
            business={
                "status": "TRIAL",
                "plan": "TRIAL",
                "trial_ends_at": future.isoformat(),
            }
        ),
    )
    assert response.status_code == 201, response.get_json()
    garage = response.get_json()["garage"]
    assert garage["status"] == "TRIAL"
    assert garage["plan"] == "TRIAL"
    assert garage["trial_ends_at"] is not None


def test_a_business_cannot_be_onboarded_as_suspended(platform_client):
    response = platform_client.post(TENANTS, json=_payload(business={"status": "SUSPENDED"}))
    assert response.status_code == 422


def test_an_unknown_plan_is_rejected(platform_client):
    response = platform_client.post(TENANTS, json=_payload(business={"plan": "PLATINUM"}))
    assert response.status_code == 422


def test_closing_before_opening_is_rejected(platform_client):
    response = platform_client.post(
        TENANTS,
        json=_payload(
            opening_hours=[{"weekday": 0, "opens_at": "18:00", "closes_at": "09:00"}],
        ),
    )
    assert response.status_code == 422
    assert Garage.query.filter_by(name="Revive N Drive").first() is None


def test_duplicate_service_names_are_rejected(platform_client):
    response = platform_client.post(
        TENANTS,
        json=_payload(services=[{"name": "MOT test"}, {"name": "mot TEST"}]),
    )
    assert response.status_code == 422
    assert "Duplicate service" in response.get_json()["message"]


def test_a_booking_setting_outside_its_range_is_rejected(platform_client):
    response = platform_client.post(
        TENANTS,
        json=_payload(booking_settings={"max_advance_days": 5000}),
    )
    assert response.status_code == 422


def test_a_client_cannot_pin_a_slug(platform_client):
    """The slug is platform-generated. A request that tries to choose one is
    refused outright rather than silently ignored, so a caller is never left
    believing it set the public URL."""
    payload = _payload()
    payload["business"]["slug"] = "chosen-by-the-client"
    response = platform_client.post(TENANTS, json=payload)
    assert response.status_code == 422
    assert Garage.query.filter_by(slug="chosen-by-the-client").first() is None


# --------------------------------------------------------------------------
# The permission boundary
# --------------------------------------------------------------------------


def test_support_cannot_onboard_a_business(support_client):
    response = support_client.post(TENANTS, json=_payload())
    assert response.status_code == 403
    assert Garage.query.filter_by(name="Revive N Drive").first() is None


def test_a_garage_owner_cannot_onboard_a_business(authenticated_client):
    assert authenticated_client.post(TENANTS, json=_payload()).status_code == 403


def test_an_anonymous_caller_cannot_onboard_a_business(client):
    assert client.post(TENANTS, json=_payload()).status_code == 401


def test_support_can_read_the_configuration(support_client, onboarded):
    response = support_client.get(f"{TENANTS}/{onboarded['garage']['id']}/configuration")
    assert response.status_code == 200


def test_onboarding_is_audited(app, onboarded):
    entry = PlatformAuditLog.query.filter_by(action="tenant.create").one()
    assert entry.garage_name == "Revive N Drive"
    assert entry.admin_email == "ops@comaz.example"
    assert entry.details["owner_email"] == "owner@revive.example"
    assert sorted(entry.details["services"]) == ["Full service", "MOT test"]
    # The audit trail never carries a credential.
    assert not any("password" in key.lower() for key in entry.details)


# --------------------------------------------------------------------------
# The tenant is immediately real to the rest of the product
# --------------------------------------------------------------------------


def test_the_public_booking_page_resolves_the_new_business(client, onboarded):
    response = client.get(f"/api/public/{onboarded['garage']['slug']}")
    assert response.status_code == 200
    body = response.get_json()
    assert body["name"] == "Revive N Drive"
    assert {service["name"] for service in body["appointment_types"]} == {
        "MOT test",
        "Full service",
    }


def test_the_hours_set_at_onboarding_drive_public_availability(client, onboarded):
    """Sunday was configured closed, so the availability API must offer no
    slots for it - the same rows, the same engine, no onboarding-only copy."""
    slug = onboarded["garage"]["slug"]
    today = datetime.now(UTC).date()
    sunday = today + timedelta(days=(6 - today.weekday()) % 7 or 7)

    response = client.get(f"/api/public/{slug}/availability/{sunday.isoformat()}")
    assert response.status_code == 200
    assert response.get_json()["slots"] == []


def test_the_booking_window_set_at_onboarding_is_the_one_the_public_api_uses(app, onboarded):
    settings = GarageScheduleSettings.query.filter_by(garage_id=onboarded["garage"]["id"]).one()
    assert settings.min_lead_time_hours == 4
    assert settings.max_advance_days == 45
    assert settings.capacity_per_slot == 2


def test_the_tenant_cannot_reach_platform_owned_identity_or_lifecycle(client, onboarded):
    """Onboarding supporting these fields must not open any of them to the tenant.

    The slug and layout are platform identity; plan, status, trial expiry and
    internal notes are platform lifecycle. None are in the garage API's own
    update schema, so a signed-in owner naming one gets a 422 and nothing
    moves. (Contact details *are* owner-editable today - see #106 - so this
    asserts the boundary that actually holds, not the one four docstrings
    claim.)
    """
    raw = _capture_invite_token(None, onboarded["owner"]["id"])
    client.post("/api/auth/reset-password", json={"token": raw, "password": "Sunflower-92-Bridge"})
    login = client.post(
        "/api/auth/login",
        json={"email": "owner@revive.example", "password": "Sunflower-92-Bridge"},
    )
    assert login.status_code == 200, login.get_json()
    auth = {"Authorization": f"Bearer {login.get_json()['access_token']}"}

    # The owner is signed in and can read their own business.
    assert client.get("/api/garage", headers=auth).status_code == 200

    before = db.session.get(Garage, onboarded["garage"]["id"])
    original = {
        "slug": before.slug,
        "layout_variant": before.layout_variant,
        "plan": before.plan,
        "status": before.status,
        "internal_notes": before.internal_notes,
    }

    for field, value in (
        ("slug", "chosen-by-the-tenant"),
        ("layout_variant", "default"),
        ("plan", "PRO"),
        ("status", "ACTIVE"),
        ("trial_ends_at", "2030-01-01T00:00:00+00:00"),
        ("internal_notes", "rewritten by the tenant"),
    ):
        response = client.patch("/api/garage", json={field: value}, headers=auth)
        assert response.status_code == 422, f"{field} was accepted: {response.get_json()}"

    db.session.expire_all()
    after = db.session.get(Garage, onboarded["garage"]["id"])
    assert {key: getattr(after, key) for key in original} == original
    assert after.trial_ends_at is None


# --------------------------------------------------------------------------
# Onboarding stage
# --------------------------------------------------------------------------


def test_a_freshly_onboarded_business_is_waiting_on_communications(platform_client, onboarded):
    assert onboarded["onboarding"]["stage"] == "core_setup_complete"
    assert onboarded["onboarding"]["stage_label"] == "Core setup complete"


def test_a_business_with_nothing_set_up_is_not_started(platform_client, garage):
    """The bare `garage` fixture has contact details but no services and
    untouched default hours."""
    row = platform_client.get(f"{TENANTS}/{garage.id}").get_json()
    assert row["onboarding"]["stage"] in ("not_started", "in_progress")


def test_live_communications_make_a_business_ready_for_launch(
    app, platform_client, onboarded, session
):
    from app.models.communications.garage_communication_settings import (
        GarageCommunicationSettings,
    )

    session.add(
        GarageCommunicationSettings(
            garage_id=onboarded["garage"]["id"],
            communications_enabled=True,
            voice_phone_number="+441234567890",
            whatsapp_sender="whatsapp:+441234567890",
        )
    )
    session.commit()

    row = platform_client.get(f"{TENANTS}/{onboarded['garage']['id']}").get_json()
    assert row["onboarding"]["stage"] == "ready_for_launch"


def test_partial_communications_are_reported_as_pending(app, platform_client, onboarded, session):
    from app.models.communications.garage_communication_settings import (
        GarageCommunicationSettings,
    )

    session.add(
        GarageCommunicationSettings(
            garage_id=onboarded["garage"]["id"],
            twilio_subaccount_sid="AC" + "0" * 32,
        )
    )
    session.commit()

    row = platform_client.get(f"{TENANTS}/{onboarded['garage']['id']}").get_json()
    assert row["onboarding"]["stage"] == "communications_pending"


def test_the_tenant_list_can_be_filtered_by_stage(platform_client, onboarded, garage):
    response = platform_client.get(f"{TENANTS}?stage=core_setup_complete")
    assert response.status_code == 200
    names = [item["garage"]["name"] for item in response.get_json()["items"]]
    assert "Revive N Drive" in names
    assert "Garage A" not in names


def test_an_unknown_stage_filter_is_rejected(platform_client):
    assert platform_client.get(f"{TENANTS}?stage=nearly_there").status_code == 422


# --------------------------------------------------------------------------
# Next setup tasks - reported, never blocking
# --------------------------------------------------------------------------


def test_next_tasks_are_reported_without_blocking_completion(onboarded):
    tasks = {task["key"]: task for task in onboarded["next_tasks"]}
    assert set(tasks) == {
        "whatsapp",
        "phone",
        "mot_reminders",
        "test_booking",
        "owner_login",
        "qr_code",
        "launch",
    }
    assert tasks["whatsapp"]["complete"] is False
    assert tasks["owner_login"]["complete"] is False
    # None of them stopped the business from being created.
    assert onboarded["created"] is True


# --------------------------------------------------------------------------
# Correcting the configuration afterwards
# --------------------------------------------------------------------------


def test_a_service_can_be_added_edited_and_removed(app, platform_client, onboarded):
    tenant_id = onboarded["garage"]["id"]

    created = platform_client.post(
        f"{TENANTS}/{tenant_id}/services",
        json={"name": "Air-con regas", "base_price": "69.00", "default_duration_minutes": 40},
    )
    assert created.status_code == 201
    service_id = created.get_json()["id"]

    updated = platform_client.patch(
        f"{TENANTS}/{tenant_id}/services/{service_id}",
        json={"base_price": "75.00", "status": "HIDDEN"},
    )
    assert updated.status_code == 200
    assert updated.get_json()["base_price"] == "75.00"
    assert updated.get_json()["status"] == "HIDDEN"

    removed = platform_client.delete(f"{TENANTS}/{tenant_id}/services/{service_id}")
    assert removed.status_code == 200
    assert db.session.get(GarageAppointmentType, service_id) is None


def test_a_duplicate_service_name_is_refused_after_onboarding(platform_client, onboarded):
    response = platform_client.post(
        f"{TENANTS}/{onboarded['garage']['id']}/services",
        json={"name": "mot test"},
    )
    assert response.status_code == 422


def test_a_service_belonging_to_another_business_is_not_reachable(
    platform_client, onboarded, garage, session
):
    other = GarageAppointmentType(garage_id=garage.id, name="Someone else's service")
    session.add(other)
    session.commit()

    response = platform_client.patch(
        f"{TENANTS}/{onboarded['garage']['id']}/services/{other.id}",
        json={"name": "Renamed"},
    )
    assert response.status_code == 404
    session.refresh(other)
    assert other.name == "Someone else's service"


def test_opening_hours_can_be_corrected(app, platform_client, onboarded):
    response = platform_client.put(
        f"{TENANTS}/{onboarded['garage']['id']}/opening-hours",
        json={"opening_hours": [{"weekday": 6, "opens_at": "10:00", "closes_at": "16:00"}]},
    )
    assert response.status_code == 200
    sunday = next(day for day in response.get_json()["opening_hours"] if day["weekday"] == 6)
    assert sunday["is_closed"] is False
    assert sunday["opens_at"].startswith("10:00")

    row = GarageOpeningHours.query.filter_by(garage_id=onboarded["garage"]["id"], weekday=6).one()
    assert row.is_closed is False


def test_correcting_hours_rejects_an_invalid_range(platform_client, onboarded):
    response = platform_client.put(
        f"{TENANTS}/{onboarded['garage']['id']}/opening-hours",
        json={"opening_hours": [{"weekday": 1, "opens_at": "17:00", "closes_at": "09:00"}]},
    )
    assert response.status_code == 422


def test_booking_settings_can_be_corrected(app, platform_client, onboarded):
    response = platform_client.put(
        f"{TENANTS}/{onboarded['garage']['id']}/booking-settings",
        json={"min_lead_time_hours": 24, "capacity_per_slot": None},
    )
    assert response.status_code == 200
    settings = response.get_json()["booking_settings"]
    assert settings["min_lead_time_hours"] == 24
    assert settings["capacity_per_slot"] is None


def test_support_cannot_correct_the_configuration(support_client, onboarded):
    tenant_id = onboarded["garage"]["id"]
    assert (
        support_client.post(f"{TENANTS}/{tenant_id}/services", json={"name": "X"}).status_code
        == 403
    )
    assert (
        support_client.put(
            f"{TENANTS}/{tenant_id}/booking-settings", json={"min_lead_time_hours": 1}
        ).status_code
        == 403
    )
    assert support_client.post(f"{TENANTS}/{tenant_id}/owner-invite").status_code == 403


def test_corrections_are_audited(app, platform_client, onboarded):
    tenant_id = onboarded["garage"]["id"]
    platform_client.put(f"{TENANTS}/{tenant_id}/booking-settings", json={"min_lead_time_hours": 12})
    platform_client.post(f"{TENANTS}/{tenant_id}/services", json={"name": "Brake check"})

    actions = {entry.action for entry in PlatformAuditLog.query.all()}
    assert "tenant.booking_settings.update" in actions
    assert "tenant.service.create" in actions


def test_resending_an_invite_voids_the_previous_link(app, platform_client, onboarded):
    first = PasswordResetToken.query.filter_by(employee_id=onboarded["owner"]["id"]).one()

    response = platform_client.post(f"{TENANTS}/{onboarded['garage']['id']}/owner-invite")
    assert response.status_code == 200
    assert response.get_json()["owner_invite"]["state"] == "sent"

    db.session.refresh(first)
    assert first.used_at is not None
    assert (
        PasswordResetToken.query.filter_by(
            employee_id=onboarded["owner"]["id"], used_at=None
        ).count()
        == 1
    )
