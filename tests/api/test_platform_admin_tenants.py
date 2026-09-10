"""Tenant management: listing, configuration, suspension, feature flags.

Two themes run through this file: every change is audited, and suspending a
tenant cuts its staff off *without* touching its data or weakening anyone
else's isolation.
"""

from datetime import UTC, datetime, timedelta

from app.models.garage import GARAGE_STATUS_SUSPENDED, Garage
from app.models.platform.audit_log import (
    ACTION_FEATURE_FLAG_UPDATE,
    ACTION_TENANT_PLAN_CHANGE,
    ACTION_TENANT_REACTIVATE,
    ACTION_TENANT_SUSPEND,
    ACTION_TENANT_UPDATE,
    PlatformAuditLog,
)
from tests.conftest import DEFAULT_PASSWORD


def test_list_returns_every_tenant(platform_client, garage, second_garage):
    response = platform_client.get("/api/platform-admin/tenants")

    assert response.status_code == 200
    names = {item["garage"]["name"] for item in response.json["items"]}
    assert names == {garage.name, second_garage.name}
    assert response.json["total"] == 2


def test_list_row_carries_signup_date_status_and_onboarding(platform_client, garage):
    item = platform_client.get("/api/platform-admin/tenants").json["items"][0]

    assert item["garage"]["created_at"]
    assert item["garage"]["status"] == "ACTIVE"
    assert item["garage"]["plan"] == "STANDARD"
    assert item["onboarding"]["total_required"] > 0
    assert 0 <= item["onboarding"]["percent_complete"] <= 100


def test_list_includes_the_owner_email(platform_client, garage, user):
    item = platform_client.get("/api/platform-admin/tenants").json["items"][0]

    assert item["owner_email"] == user.email


def test_search_matches_name_slug_and_owner_email(platform_client, garage, second_garage, user):
    by_name = platform_client.get("/api/platform-admin/tenants?search=Garage A")
    by_slug = platform_client.get("/api/platform-admin/tenants?search=garage-b")
    by_owner = platform_client.get(f"/api/platform-admin/tenants?search={user.email}")

    assert [i["garage"]["slug"] for i in by_name.json["items"]] == ["garage-a"]
    assert [i["garage"]["slug"] for i in by_slug.json["items"]] == ["garage-b"]
    assert [i["garage"]["slug"] for i in by_owner.json["items"]] == ["garage-a"]


def test_filter_by_status(platform_client, garage, second_garage, session):
    second_garage.status = GARAGE_STATUS_SUSPENDED
    session.commit()

    response = platform_client.get("/api/platform-admin/tenants?status=SUSPENDED")

    assert [i["garage"]["slug"] for i in response.json["items"]] == ["garage-b"]


def test_pagination_reports_totals(platform_client, garage, second_garage):
    response = platform_client.get("/api/platform-admin/tenants?per_page=1&page=2")

    assert response.json["total"] == 2
    assert response.json["pages"] == 2
    assert len(response.json["items"]) == 1


def test_tenant_detail_shows_configuration_and_counts(platform_client, garage, customer, vehicle):
    response = platform_client.get(f"/api/platform-admin/tenants/{garage.id}")

    assert response.status_code == 200
    assert response.json["garage"]["slug"] == garage.slug
    assert response.json["customer_count"] == 1
    assert response.json["vehicle_count"] == 1


def test_unknown_tenant_is_404(platform_client):
    missing = "00000000-0000-0000-0000-000000000000"

    assert platform_client.get(f"/api/platform-admin/tenants/{missing}").status_code == 404


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def test_superadmin_can_edit_tenant_details(platform_client, garage):
    response = platform_client.patch(
        f"/api/platform-admin/tenants/{garage.id}",
        json={"name": "Garage A (renamed)", "phone": "+44 20 7946 9999"},
    )

    assert response.status_code == 200
    assert response.json["garage"]["name"] == "Garage A (renamed)"
    assert response.json["garage"]["phone"] == "+44 20 7946 9999"


def test_editing_a_tenant_is_audited_with_before_and_after(platform_client, garage, platform_admin):
    platform_client.patch(
        f"/api/platform-admin/tenants/{garage.id}", json={"name": "Garage A (renamed)"}
    )

    entry = PlatformAuditLog.query.filter_by(action=ACTION_TENANT_UPDATE).one()
    assert entry.admin_email == platform_admin.email
    assert entry.garage_id == garage.id
    assert entry.details["changed"]["name"] == {
        "from": "Garage A",
        "to": "Garage A (renamed)",
    }


def test_the_slug_is_not_editable(platform_client, garage):
    response = platform_client.patch(
        f"/api/platform-admin/tenants/{garage.id}", json={"slug": "hijacked"}
    )

    assert response.status_code == 422
    assert garage.slug == "garage-a"


def test_status_cannot_be_changed_through_the_config_endpoint(platform_client, garage):
    """Suspension is its own audited operation, not a field edit."""
    response = platform_client.patch(
        f"/api/platform-admin/tenants/{garage.id}", json={"status": "SUSPENDED"}
    )

    assert response.status_code == 422
    assert garage.status == "ACTIVE"


def test_an_empty_update_is_rejected(platform_client, garage):
    assert (
        platform_client.patch(f"/api/platform-admin/tenants/{garage.id}", json={}).status_code
        == 422
    )


def test_changing_the_plan_is_audited_as_a_plan_change(platform_client, garage):
    response = platform_client.patch(
        f"/api/platform-admin/tenants/{garage.id}", json={"plan": "PRO"}
    )

    assert response.status_code == 200
    assert response.json["garage"]["plan"] == "PRO"
    assert PlatformAuditLog.query.filter_by(action=ACTION_TENANT_PLAN_CHANGE).count() == 1


def test_an_unknown_plan_is_rejected(platform_client, garage):
    response = platform_client.patch(
        f"/api/platform-admin/tenants/{garage.id}", json={"plan": "ENTERPRISE"}
    )

    assert response.status_code == 422


def test_trial_end_and_internal_notes_are_platform_only_fields(platform_client, garage):
    ends = (datetime.now(UTC) + timedelta(days=14)).isoformat()

    response = platform_client.patch(
        f"/api/platform-admin/tenants/{garage.id}",
        json={"trial_ends_at": ends, "internal_notes": "Referred by MazTrad."},
    )

    assert response.status_code == 200
    assert response.json["garage"]["internal_notes"] == "Referred by MazTrad."
    assert response.json["garage"]["trial_ends_at"] is not None


def test_the_garage_api_never_exposes_platform_fields(authenticated_client, garage, session):
    """A garage user must not be able to see the platform's view of itself."""
    garage.internal_notes = "Chasing unpaid invoice."
    garage.status = "TRIAL"
    session.commit()

    body = authenticated_client.get("/api/garage").json

    assert "internal_notes" not in body
    assert "status" not in body
    assert "plan" not in body
    assert "suspension_reason" not in body


# --------------------------------------------------------------------------
# Suspension
# --------------------------------------------------------------------------


def test_suspending_a_tenant_records_the_reason(platform_client, garage):
    response = platform_client.post(
        f"/api/platform-admin/tenants/{garage.id}/suspend",
        json={"reason": "Non-payment: invoice 41 overdue 60 days."},
    )

    assert response.status_code == 200
    assert response.json["garage"]["status"] == "SUSPENDED"
    assert response.json["garage"]["suspension_reason"].startswith("Non-payment")

    entry = PlatformAuditLog.query.filter_by(action=ACTION_TENANT_SUSPEND).one()
    assert "Non-payment" in entry.details["reason"]


def test_suspension_requires_a_reason(platform_client, garage):
    response = platform_client.post(
        f"/api/platform-admin/tenants/{garage.id}/suspend", json={"reason": "   "}
    )

    assert response.status_code == 422
    assert garage.status == "ACTIVE"


def test_a_suspended_tenants_staff_cannot_log_in(platform_client, client, garage, user):
    platform_client.post(
        f"/api/platform-admin/tenants/{garage.id}/suspend", json={"reason": "Non-payment."}
    )

    response = client.post(
        "/api/auth/login", json={"email": user.email, "password": DEFAULT_PASSWORD}
    )

    assert response.status_code == 403
    assert "suspended" in response.json["message"].lower()


def test_suspension_kills_a_live_staff_token(platform_client, authenticated_client, garage):
    assert authenticated_client.get("/api/garage").status_code == 200

    platform_client.post(
        f"/api/platform-admin/tenants/{garage.id}/suspend", json={"reason": "Non-payment."}
    )

    # Same token, next request.
    assert authenticated_client.get("/api/garage").status_code == 401


def test_suspension_does_not_affect_another_tenant(
    platform_client, second_authenticated_client, garage, second_garage
):
    platform_client.post(
        f"/api/platform-admin/tenants/{garage.id}/suspend", json={"reason": "Non-payment."}
    )

    assert second_authenticated_client.get("/api/garage").status_code == 200


def test_suspension_deletes_nothing(platform_client, garage, customer, vehicle, session):
    platform_client.post(
        f"/api/platform-admin/tenants/{garage.id}/suspend", json={"reason": "Non-payment."}
    )

    session.expire_all()
    assert session.get(Garage, garage.id) is not None
    detail = platform_client.get(f"/api/platform-admin/tenants/{garage.id}").json
    assert detail["customer_count"] == 1
    assert detail["vehicle_count"] == 1


def test_suspending_twice_is_rejected(platform_client, garage):
    platform_client.post(
        f"/api/platform-admin/tenants/{garage.id}/suspend", json={"reason": "Non-payment."}
    )
    again = platform_client.post(
        f"/api/platform-admin/tenants/{garage.id}/suspend", json={"reason": "Non-payment."}
    )

    assert again.status_code == 422


def test_reactivating_restores_access_and_clears_the_reason(
    platform_client, authenticated_client, client, garage, user
):
    platform_client.post(
        f"/api/platform-admin/tenants/{garage.id}/suspend", json={"reason": "Non-payment."}
    )

    response = platform_client.post(
        f"/api/platform-admin/tenants/{garage.id}/reactivate", json={"status": "ACTIVE"}
    )

    assert response.status_code == 200
    assert response.json["garage"]["status"] == "ACTIVE"
    assert response.json["garage"]["suspension_reason"] is None
    assert PlatformAuditLog.query.filter_by(action=ACTION_TENANT_REACTIVATE).count() == 1

    login = client.post("/api/auth/login", json={"email": user.email, "password": DEFAULT_PASSWORD})
    assert login.status_code == 200


def test_reactivating_a_tenant_that_is_not_suspended_is_rejected(platform_client, garage):
    response = platform_client.post(f"/api/platform-admin/tenants/{garage.id}/reactivate", json={})

    assert response.status_code == 422


# --------------------------------------------------------------------------
# Onboarding progress + feature flags
# --------------------------------------------------------------------------


def test_onboarding_progress_is_derived_from_real_data(
    platform_client, garage, appointment_type, customer
):
    response = platform_client.get(f"/api/platform-admin/tenants/{garage.id}/onboarding")

    assert response.status_code == 200
    by_key = {step["key"]: step for step in response.json["steps"]}
    assert by_key["services"]["complete"] is True
    assert by_key["customers"]["complete"] is True
    assert by_key["appointments"]["complete"] is False
    assert by_key["business_details"]["complete"] is True


def test_optional_steps_do_not_hold_back_the_percentage(platform_client, garage):
    response = platform_client.get(f"/api/platform-admin/tenants/{garage.id}/onboarding")

    optional = [s for s in response.json["steps"] if not s["required"]]
    assert {s["key"] for s in optional} == {"team", "communications"}
    assert response.json["total_required"] == len(
        [s for s in response.json["steps"] if s["required"]]
    )


def test_feature_flags_default_to_the_plan(platform_client, garage):
    response = platform_client.get(f"/api/platform-admin/tenants/{garage.id}/feature-flags")

    assert response.status_code == 200
    assert response.json["plan"] == "STANDARD"
    flags = {flag["key"]: flag for flag in response.json["flags"]}
    assert flags["public_booking"]["enabled"] is True
    assert flags["public_booking"]["source"] == "plan"
    # Not in the STANDARD plan.
    assert flags["voice_assistant"]["enabled"] is False


def test_an_override_wins_over_the_plan_and_is_audited(platform_client, garage):
    response = platform_client.put(
        f"/api/platform-admin/tenants/{garage.id}/feature-flags",
        json={"key": "voice_assistant", "enabled": True},
    )

    assert response.status_code == 200
    flags = {flag["key"]: flag for flag in response.json["flags"]}
    assert flags["voice_assistant"]["enabled"] is True
    assert flags["voice_assistant"]["source"] == "override"
    assert PlatformAuditLog.query.filter_by(action=ACTION_FEATURE_FLAG_UPDATE).count() == 1


def test_clearing_an_override_returns_the_flag_to_its_plan_default(platform_client, garage):
    platform_client.put(
        f"/api/platform-admin/tenants/{garage.id}/feature-flags",
        json={"key": "voice_assistant", "enabled": True},
    )

    response = platform_client.put(
        f"/api/platform-admin/tenants/{garage.id}/feature-flags",
        json={"key": "voice_assistant", "enabled": None},
    )

    flags = {flag["key"]: flag for flag in response.json["flags"]}
    assert flags["voice_assistant"]["enabled"] is False
    assert flags["voice_assistant"]["source"] == "plan"


def test_an_unknown_feature_flag_is_rejected(platform_client, garage):
    response = platform_client.put(
        f"/api/platform-admin/tenants/{garage.id}/feature-flags",
        json={"key": "teleportation", "enabled": True},
    )

    assert response.status_code == 422


def test_changing_the_plan_moves_every_un_overridden_flag(platform_client, garage):
    platform_client.patch(f"/api/platform-admin/tenants/{garage.id}", json={"plan": "PRO"})

    flags = {
        flag["key"]: flag
        for flag in platform_client.get(
            f"/api/platform-admin/tenants/{garage.id}/feature-flags"
        ).json["flags"]
    }
    assert flags["voice_assistant"]["enabled"] is True
    assert flags["voice_assistant"]["source"] == "plan"
