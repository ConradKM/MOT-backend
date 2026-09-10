"""Support impersonation.

The properties this file pins down are the ones the feature would be
dangerous without: the owner's password is never involved, the grant is
short-lived and single-use, revoking it takes effect on the next request, an
impersonation token can't climb back into Platform Admin, and every start and
stop is audited with the reason the admin gave.
"""

from datetime import UTC, datetime, timedelta

import pytest
from flask_jwt_extended import decode_token

from app.models.platform.audit_log import (
    ACTION_IMPERSONATION_REVOKE,
    ACTION_IMPERSONATION_START,
    PlatformAuditLog,
)
from app.models.platform.impersonation import ImpersonationSession
from app.platform_admin.impersonation import (
    CLAIM_ADMIN_EMAIL,
    CLAIM_ADMIN_ID,
    CLAIM_SESSION_ID,
)

REASON = "Owner reports the booking calendar is empty - ticket 4412."


@pytest.fixture(autouse=True)
def _tenant_has_an_owner(user):
    """Every test in this file impersonates, which requires the tenant to have
    an active OWNER account - that is the `user` fixture. Autouse, so no test
    here can accidentally assert against a tenant with no staff at all and
    mistake "nobody to impersonate" for the behaviour it meant to check."""


def _start(platform_client, garage, **overrides):
    payload = {"reason": REASON, **overrides}
    return platform_client.post(
        f"/api/platform-admin/tenants/{garage.id}/impersonate", json=payload
    )


def _code_from(response):
    """The one-time code travels in the URL *fragment*, never a query string."""
    handoff_url = response.json["handoff_url"]
    assert "#code=" in handoff_url
    assert "?code=" not in handoff_url
    return handoff_url.split("#code=", 1)[1]


def test_starting_an_impersonation_returns_a_handoff_not_a_token(platform_client, garage, user):
    response = _start(platform_client, garage)

    assert response.status_code == 201
    # The console never receives a usable garage token.
    assert "access_token" not in response.json
    assert response.json["handoff_url"]
    assert response.json["employee"]["email"] == user.email
    assert response.json["garage"]["id"] == str(garage.id)


def test_the_session_defaults_to_the_tenants_owner(platform_client, garage, user):
    _start(platform_client, garage)

    session_row = ImpersonationSession.query.one()
    assert session_row.employee_id == user.id
    assert session_row.garage_id == garage.id


def test_a_reason_is_required_and_recorded(platform_client, garage):
    too_short = _start(platform_client, garage, reason="oops")
    assert too_short.status_code == 422

    _start(platform_client, garage)
    entry = PlatformAuditLog.query.filter_by(action=ACTION_IMPERSONATION_START).one()
    assert entry.details["reason"] == REASON
    assert entry.garage_id == garage.id


def test_the_owners_password_is_never_read_or_returned(platform_client, garage, user):
    original_hash = user.password_hash

    response = _start(platform_client, garage)

    assert original_hash == user.password_hash
    body = response.get_data(as_text=True)
    assert "password" not in body.lower()
    assert original_hash not in body


def test_exchanging_the_code_yields_a_working_garage_token(client, platform_client, garage, user):
    code = _code_from(_start(platform_client, garage))

    exchange = client.post("/api/auth/impersonation/exchange", json={"code": code})

    assert exchange.status_code == 200
    token = exchange.json["access_token"]
    assert exchange.json["impersonated_by_email"] == "ops@comaz.example"

    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json["email"] == user.email


def test_the_exchange_returns_no_refresh_token(client, platform_client, garage):
    code = _code_from(_start(platform_client, garage))

    exchange = client.post("/api/auth/impersonation/exchange", json={"code": code})

    assert "refresh_token" not in exchange.json


def test_a_code_can_only_be_exchanged_once(client, platform_client, garage):
    code = _code_from(_start(platform_client, garage))

    first = client.post("/api/auth/impersonation/exchange", json={"code": code})
    second = client.post("/api/auth/impersonation/exchange", json={"code": code})

    assert first.status_code == 200
    assert second.status_code == 400


def test_an_unknown_code_and_a_used_code_look_identical(client, platform_client, garage):
    code = _code_from(_start(platform_client, garage))
    client.post("/api/auth/impersonation/exchange", json={"code": code})

    used = client.post("/api/auth/impersonation/exchange", json={"code": code})
    unknown = client.post("/api/auth/impersonation/exchange", json={"code": "x" * 43})

    assert used.status_code == unknown.status_code == 400
    assert used.json["message"] == unknown.json["message"]


def test_an_expired_handoff_code_is_refused(client, platform_client, garage, session):
    code = _code_from(_start(platform_client, garage))

    row = ImpersonationSession.query.one()
    row.handoff_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    session.commit()

    assert client.post("/api/auth/impersonation/exchange", json={"code": code}).status_code == 400


def test_the_code_is_not_stored_in_plaintext(platform_client, garage):
    code = _code_from(_start(platform_client, garage))

    row = ImpersonationSession.query.one()
    assert row.handoff_code_hash != code
    assert len(row.handoff_code_hash) == 64


def test_the_token_carries_impersonation_claims_for_the_banner(
    client, platform_client, garage, platform_admin
):
    code = _code_from(_start(platform_client, garage))
    token = client.post("/api/auth/impersonation/exchange", json={"code": code}).json[
        "access_token"
    ]

    claims = decode_token(token)
    assert claims[CLAIM_ADMIN_ID] == str(platform_admin.id)
    assert claims[CLAIM_ADMIN_EMAIL] == platform_admin.email
    assert claims[CLAIM_SESSION_ID]


def test_an_impersonation_token_cannot_reach_platform_admin(client, platform_client, garage):
    """The escalation that must never work: in as the tenant, back out as the
    platform."""
    code = _code_from(_start(platform_client, garage))
    token = client.post("/api/auth/impersonation/exchange", json={"code": code}).json[
        "access_token"
    ]

    response = client.get(
        "/api/platform-admin/tenants", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 403


def test_an_impersonation_token_is_still_scoped_to_its_own_tenant(
    client, platform_client, garage, second_garage, second_customer
):
    """Impersonation grants the tenant's own access - never a wider one."""
    code = _code_from(_start(platform_client, garage))
    token = client.post("/api/auth/impersonation/exchange", json={"code": code}).json[
        "access_token"
    ]

    headers = {"Authorization": f"Bearer {token}"}
    own = client.get("/api/garage", headers=headers)
    other = client.get(f"/api/customers/{second_customer.id}", headers=headers)

    assert own.json["id"] == str(garage.id)
    assert other.status_code == 404


def test_revoking_kills_the_token_on_the_next_request(client, platform_client, garage):
    code = _code_from(_start(platform_client, garage))
    token = client.post("/api/auth/impersonation/exchange", json={"code": code}).json[
        "access_token"
    ]
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/garage", headers=headers).status_code == 200

    session_row = ImpersonationSession.query.one()
    revoke = platform_client.post(
        f"/api/platform-admin/impersonation-sessions/{session_row.id}/revoke"
    )

    assert revoke.status_code == 200
    assert client.get("/api/garage", headers=headers).status_code == 401
    assert PlatformAuditLog.query.filter_by(action=ACTION_IMPERSONATION_REVOKE).count() == 1


def test_revoking_is_idempotent(platform_client, garage):
    _start(platform_client, garage)
    session_row = ImpersonationSession.query.one()

    first = platform_client.post(
        f"/api/platform-admin/impersonation-sessions/{session_row.id}/revoke"
    )
    second = platform_client.post(
        f"/api/platform-admin/impersonation-sessions/{session_row.id}/revoke"
    )

    assert first.status_code == second.status_code == 200
    assert PlatformAuditLog.query.filter_by(action=ACTION_IMPERSONATION_REVOKE).count() == 1


def test_an_expired_session_stops_working(client, platform_client, garage, session):
    code = _code_from(_start(platform_client, garage))
    token = client.post("/api/auth/impersonation/exchange", json={"code": code}).json[
        "access_token"
    ]
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/garage", headers=headers).status_code == 200

    row = ImpersonationSession.query.one()
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    session.commit()

    assert client.get("/api/garage", headers=headers).status_code == 401


def test_support_admins_may_impersonate(support_client, garage):
    """Support is read-only on configuration but needs impersonation by
    definition - it is the whole point of the role."""
    response = support_client.post(
        f"/api/platform-admin/tenants/{garage.id}/impersonate", json={"reason": REASON}
    )

    assert response.status_code == 201


def test_a_named_employee_must_belong_to_the_tenant(platform_client, garage, second_user):
    response = _start(platform_client, garage, employee_id=str(second_user.id))

    assert response.status_code == 422
    assert "does not belong" in response.json["message"]


def test_a_deactivated_employee_cannot_be_impersonated(platform_client, garage, user, session):
    user.is_active = False
    session.commit()

    response = _start(platform_client, garage, employee_id=str(user.id))

    assert response.status_code == 422


def test_impersonation_works_on_a_suspended_tenant(client, platform_client, garage):
    """Deliberate: support has to be able to get in to fix what caused the
    suspension. Ordinary staff logins stay blocked."""
    platform_client.post(
        f"/api/platform-admin/tenants/{garage.id}/suspend", json={"reason": "Non-payment."}
    )

    code = _code_from(_start(platform_client, garage))
    token = client.post("/api/auth/impersonation/exchange", json={"code": code}).json[
        "access_token"
    ]

    assert (
        client.get("/api/garage", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    )


def test_sessions_are_listed_for_review(platform_client, garage):
    _start(platform_client, garage)

    response = platform_client.get("/api/platform-admin/impersonation-sessions")

    assert response.status_code == 200
    assert len(response.json) == 1
    assert response.json[0]["reason"] == REASON
    assert response.json[0]["is_active"] is True


def test_active_only_filter_excludes_revoked_sessions(platform_client, garage):
    _start(platform_client, garage)
    session_row = ImpersonationSession.query.one()
    platform_client.post(f"/api/platform-admin/impersonation-sessions/{session_row.id}/revoke")

    active = platform_client.get("/api/platform-admin/impersonation-sessions?active_only=true")
    everything = platform_client.get("/api/platform-admin/impersonation-sessions")

    assert active.json == []
    assert len(everything.json) == 1
