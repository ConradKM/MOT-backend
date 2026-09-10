"""The Platform Admin permission boundary.

The most important tests in this feature. Everything else assumes the answer
to one question: can a garage or customer credential reach
``/api/platform-admin``? These say no, from every direction - wrong token
type, wrong role, deactivated account, revoked-by-password-change - and prove
that the boundary is re-checked against the database on every request rather
than trusted from a claim.
"""

from datetime import UTC, datetime, timedelta

import pytest
from flask_jwt_extended import create_access_token

from app.models.platform.audit_log import (
    ACTION_LOGIN,
    ACTION_LOGIN_FAILED,
    PlatformAuditLog,
)
from tests.conftest import DEFAULT_PASSWORD

# One representative route per privilege level, so the parametrised boundary
# tests below cover the whole namespace without listing every endpoint.
READ_ROUTES = (
    "/api/platform-admin/auth/me",
    "/api/platform-admin/tenants",
    "/api/platform-admin/stats/overview",
    "/api/platform-admin/audit-logs",
    "/api/platform-admin/operations/jobs",
)


def test_platform_admin_can_sign_in(client, platform_admin):
    response = client.post(
        "/api/platform-admin/auth/login",
        json={"email": platform_admin.email, "password": DEFAULT_PASSWORD},
    )

    assert response.status_code == 200
    assert response.json["access_token"]
    assert response.json["refresh_token"]


def test_sign_in_records_an_audit_entry(client, platform_admin):
    client.post(
        "/api/platform-admin/auth/login",
        json={"email": platform_admin.email, "password": DEFAULT_PASSWORD},
    )

    entry = PlatformAuditLog.query.filter_by(action=ACTION_LOGIN).one()
    assert entry.admin_id == platform_admin.id
    assert entry.admin_email == platform_admin.email


def test_failed_sign_in_is_audited_without_the_password(client, platform_admin):
    response = client.post(
        "/api/platform-admin/auth/login",
        json={"email": platform_admin.email, "password": "wrong-password"},
    )

    assert response.status_code == 401
    entry = PlatformAuditLog.query.filter_by(action=ACTION_LOGIN_FAILED).one()
    assert entry.admin_email == platform_admin.email
    assert "wrong-password" not in (str(entry.details) + str(entry.summary))


def test_unknown_admin_and_wrong_password_look_identical(client, platform_admin):
    unknown = client.post(
        "/api/platform-admin/auth/login",
        json={"email": "nobody@comaz.example", "password": DEFAULT_PASSWORD},
    )
    wrong = client.post(
        "/api/platform-admin/auth/login",
        json={"email": platform_admin.email, "password": "nope-nope-nope"},
    )

    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json["message"] == wrong.json["message"]


def test_a_garage_owner_cannot_sign_in_to_platform_admin(client, user):
    """The garage owner's real credentials, against the platform login."""
    response = client.post(
        "/api/platform-admin/auth/login",
        json={"email": user.email, "password": DEFAULT_PASSWORD},
    )

    assert response.status_code == 401


def test_a_deactivated_admin_cannot_sign_in(client, platform_admin, session):
    platform_admin.is_active = False
    session.commit()

    response = client.post(
        "/api/platform-admin/auth/login",
        json={"email": platform_admin.email, "password": DEFAULT_PASSWORD},
    )

    assert response.status_code == 401


@pytest.mark.parametrize("route", READ_ROUTES)
def test_platform_routes_reject_an_anonymous_caller(client, route):
    assert client.get(route).status_code == 401


@pytest.mark.parametrize("route", READ_ROUTES)
def test_platform_routes_reject_a_garage_employee_token(authenticated_client, route):
    """A perfectly valid garage token: authenticated, but never as the platform."""
    response = authenticated_client.get(route)

    assert response.status_code == 403
    assert "administrator" in response.json["message"].lower()


@pytest.mark.parametrize("route", READ_ROUTES)
def test_platform_routes_reject_a_customer_token(customer_client, route):
    assert customer_client.get(route).status_code == 403


def test_a_forged_platform_claim_on_an_employee_id_is_rejected(app, client, user):
    """The claim alone proves nothing: the subject must resolve to a real row
    in `platform_admins`, and an employee id never will.

    401 rather than the 403 a real-but-unprivileged caller gets, because the
    JWT blocklist loader rejects this token before the view is ever reached -
    an earlier and stricter refusal.
    """
    token = create_access_token(
        identity=str(user.id), additional_claims={"account_type": "platform_admin"}
    )

    response = client.get(
        "/api/platform-admin/tenants", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 401


def test_platform_routes_accept_a_platform_admin(platform_client):
    response = platform_client.get("/api/platform-admin/auth/me")

    assert response.status_code == 200
    assert response.json["role"] == "SUPERADMIN"
    assert response.json["is_superadmin"] is True
    # A response about an account must never carry its secret material.
    assert "password_hash" not in response.json
    assert "password" not in response.json


def test_deactivating_an_admin_kills_its_live_token(platform_client, platform_admin, session):
    assert platform_client.get("/api/platform-admin/tenants").status_code == 200

    platform_admin.is_active = False
    session.commit()

    # Same token, next request - the blocklist loader re-reads the row.
    assert platform_client.get("/api/platform-admin/tenants").status_code == 401


def test_a_password_change_invalidates_tokens_issued_before_it(
    platform_client, platform_admin, session
):
    assert platform_client.get("/api/platform-admin/tenants").status_code == 200

    platform_admin.tokens_valid_from = datetime.now(UTC) + timedelta(seconds=1)
    session.commit()

    assert platform_client.get("/api/platform-admin/tenants").status_code == 401


def test_support_admin_can_read(support_client):
    assert support_client.get("/api/platform-admin/tenants").status_code == 200


def test_support_admin_cannot_change_a_tenant(support_client, garage):
    response = support_client.patch(
        f"/api/platform-admin/tenants/{garage.id}", json={"name": "Renamed by support"}
    )

    assert response.status_code == 403
    assert "superadmin" in response.json["message"].lower()


def test_support_admin_cannot_suspend_a_tenant(support_client, garage):
    response = support_client.post(
        f"/api/platform-admin/tenants/{garage.id}/suspend", json={"reason": "Non-payment"}
    )

    assert response.status_code == 403


def test_refresh_issues_a_new_access_token(client, app, platform_admin):
    from flask_jwt_extended import create_refresh_token

    refresh = create_refresh_token(
        identity=str(platform_admin.id),
        additional_claims={"account_type": "platform_admin", "role": platform_admin.role},
    )

    response = client.post(
        "/api/platform-admin/auth/refresh", headers={"Authorization": f"Bearer {refresh}"}
    )

    assert response.status_code == 200
    assert response.json["access_token"]


def test_a_garage_refresh_token_cannot_refresh_into_platform_admin(client, refresh_token):
    response = client.post(
        "/api/platform-admin/auth/refresh", headers={"Authorization": f"Bearer {refresh_token}"}
    )

    assert response.status_code == 401


def test_identity_is_not_carried_over_between_requests(
    client, platform_client, authenticated_client, garage
):
    """Regression: a platform admin's identity must not leak into the next
    request on the same app context.

    Flask reuses the app context - and therefore ``flask.g`` - across requests
    whenever one is already pushed (the test suite, a CLI command, a nested
    ``app.app_context()``). Memoising the resolved admin on ``g`` therefore
    let a *later* request with a completely different token be treated as the
    admin who made an *earlier* one. Identity is re-resolved per request now;
    this test fails loudly if that is ever optimised away again.
    """
    assert platform_client.get("/api/platform-admin/tenants").status_code == 200

    # Same app context, different caller - must be judged on its own token.
    assert authenticated_client.get("/api/platform-admin/tenants").status_code == 403
    assert client.get("/api/platform-admin/tenants").status_code == 401
