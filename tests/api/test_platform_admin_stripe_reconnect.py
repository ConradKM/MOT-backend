"""Platform Admin's Stripe Connect recovery action for a tenant whose stored
connected account is permanently unreachable to the current CoMaz platform
(see app.payments.connect.reconnect_stripe_account, issue #271/#273).

Every test uses a small Stripe fake - never a real account or network call -
and deliberately proves the safety rails: reconnect must never run just
because a call errors, must never touch a healthy account, must never create
two active accounts for the same garage, and must never lose old payment
history.
"""

import datetime

import stripe

from app.models.booking_request import BookingRequest
from app.models.payments.garage_payment_account_history import GaragePaymentAccountHistory
from app.models.payments.garage_payment_settings import GaragePaymentSettings
from app.models.payments.payment import BookingPayment
from app.models.platform.audit_log import ACTION_TENANT_STRIPE_RECONNECT, PlatformAuditLog


class _BrokenAccountApi:
    """Every retrieve for the given (old) account id 403s exactly like the
    production incident; anything else (a freshly created account) is
    reachable, matching how Stripe actually behaves - the platform key can
    always reach an account it just created."""

    def __init__(self, broken_account_id):
        self.broken_account_id = broken_account_id
        self.retrieved = []

    def retrieve(self, account_id):
        self.retrieved.append(account_id)
        if account_id == self.broken_account_id:
            raise stripe.error.PermissionError(
                f"The provided key does not have access to account '{account_id}'.",
                http_status=403,
            )
        return {"id": account_id, "charges_enabled": False, "payouts_enabled": False}


class _FakeV2Accounts:
    def __init__(self, next_id="acct_fresh_1"):
        self.next_id = next_id
        self.created = []

    def create(self, params):
        self.created.append(params)
        return type("V2Account", (), {"id": self.next_id})()


class _FakeV2AccountLinks:
    def __init__(self):
        self.created = []

    def create(self, params):
        self.created.append(params)
        return type("V2AccountLink", (), {"url": "https://connect.stripe.test/reconnect"})()


def _patch_stripe(monkeypatch, *, broken_account_id, new_account_id="acct_fresh_1"):
    account_api = _BrokenAccountApi(broken_account_id)
    monkeypatch.setattr(stripe, "Account", account_api)
    monkeypatch.setattr("app.payments.connect._client", lambda: stripe)

    v2_accounts = _FakeV2Accounts(new_account_id)
    v2_links = _FakeV2AccountLinks()
    v2_client = type(
        "V2Client",
        (),
        {
            "v2": type(
                "V2",
                (),
                {"core": type("Core", (), {"accounts": v2_accounts, "account_links": v2_links})()},
            )()
        },
    )()
    monkeypatch.setattr("app.payments.connect._v2_client", lambda: v2_client)
    return account_api, v2_accounts, v2_links


def _broken_settings(session, garage, account_id="acct_broken_old"):
    settings = GaragePaymentSettings(
        garage_id=garage.id,
        provider="stripe",
        stripe_account_id=account_id,
        stripe_charges_enabled=True,
        stripe_payouts_enabled=True,
        stripe_onboarding_complete=True,
        stripe_details_submitted=True,
    )
    session.add(settings)
    session.commit()
    return settings


def test_reconnect_requires_a_reason(platform_client, session, garage):
    _broken_settings(session, garage)
    response = platform_client.post(
        f"/api/platform-admin/tenants/{garage.id}/payments/stripe/reconnect", json={"reason": ""}
    )
    assert response.status_code in (400, 422)


def test_reconnect_refuses_an_account_stripe_can_still_reach(
    platform_client, session, garage, monkeypatch
):
    """Reconnect exists only to recover a broken account - it must never be
    usable to reset a connection that still works."""
    settings = _broken_settings(session, garage, account_id="acct_healthy")
    _patch_stripe(monkeypatch, broken_account_id="acct_nobody_home")

    response = platform_client.post(
        f"/api/platform-admin/tenants/{garage.id}/payments/stripe/reconnect",
        json={"reason": "Just checking"},
    )

    assert response.status_code == 422
    session.refresh(settings)
    assert settings.stripe_account_id == "acct_healthy"
    assert settings.stripe_charges_enabled is True
    assert GaragePaymentAccountHistory.query.count() == 0


def test_reconnect_without_any_stored_account_is_rejected(platform_client, garage):
    response = platform_client.post(
        f"/api/platform-admin/tenants/{garage.id}/payments/stripe/reconnect",
        json={"reason": "Nothing to reconnect"},
    )
    assert response.status_code == 422


def test_reconnect_does_not_run_on_a_merely_transient_stripe_error(
    platform_client, session, garage, monkeypatch
):
    """A rate limit, a network blip, or any Stripe failure other than the
    specific "this key cannot reach that account" case must never be treated
    as "the account is broken" - reconnect only ever fires on that one
    unambiguous signal, never as a generic error fallback."""
    settings = _broken_settings(session, garage, account_id="acct_flaky")

    class _FlakyAccountApi:
        def retrieve(self, account_id):
            raise stripe.error.RateLimitError("Too many requests", http_status=429)

    monkeypatch.setattr(stripe, "Account", _FlakyAccountApi())
    monkeypatch.setattr("app.payments.connect._client", lambda: stripe)

    response = platform_client.post(
        f"/api/platform-admin/tenants/{garage.id}/payments/stripe/reconnect",
        json={"reason": "Trying anyway"},
    )

    assert response.status_code == 422
    session.refresh(settings)
    assert settings.stripe_account_id == "acct_flaky"
    assert GaragePaymentAccountHistory.query.count() == 0


def test_reconnect_detaches_the_broken_account_and_creates_a_fresh_one(
    platform_client, session, garage, monkeypatch
):
    settings = _broken_settings(session, garage, account_id="acct_broken_old")
    _, v2_accounts, v2_links = _patch_stripe(
        monkeypatch, broken_account_id="acct_broken_old", new_account_id="acct_fresh_1"
    )

    response = platform_client.post(
        f"/api/platform-admin/tenants/{garage.id}/payments/stripe/reconnect",
        json={"reason": "Stripe reports 403 for this account - see issue #271"},
    )

    assert response.status_code == 200
    assert response.json["stripe_account_id"] == "acct_fresh_1"
    assert response.json["onboarding_url"] == "https://connect.stripe.test/reconnect"

    session.refresh(settings)
    assert settings.stripe_account_id == "acct_fresh_1"
    assert settings.stripe_charges_enabled is False
    assert settings.stripe_payouts_enabled is False
    assert settings.stripe_onboarding_complete is False

    history = GaragePaymentAccountHistory.query.filter_by(garage_id=garage.id).one()
    assert history.stripe_account_id == "acct_broken_old"
    assert "271" in history.reason or "403" in history.reason

    assert len(v2_accounts.created) == 1
    assert len(v2_links.created) == 1
    assert v2_links.created[0]["account"] == "acct_fresh_1"

    entry = PlatformAuditLog.query.filter_by(action=ACTION_TENANT_STRIPE_RECONNECT).one()
    assert entry.details["new_stripe_account_id"] == "acct_fresh_1"
    assert entry.garage_id == garage.id


def test_reconnect_is_tenant_scoped(platform_client, session, garage, second_garage, monkeypatch):
    _broken_settings(session, garage, account_id="acct_broken_a")
    other_settings = _broken_settings(session, second_garage, account_id="acct_healthy_b")
    _patch_stripe(monkeypatch, broken_account_id="acct_broken_a", new_account_id="acct_fresh_a")

    response = platform_client.post(
        f"/api/platform-admin/tenants/{garage.id}/payments/stripe/reconnect",
        json={"reason": "Only this tenant is broken"},
    )

    assert response.status_code == 200
    session.refresh(other_settings)
    assert other_settings.stripe_account_id == "acct_healthy_b"
    assert other_settings.stripe_charges_enabled is True
    assert GaragePaymentAccountHistory.query.filter_by(garage_id=second_garage.id).count() == 0


def test_a_second_reconnect_after_success_is_refused_as_healthy_not_duplicated(
    platform_client, session, garage, monkeypatch
):
    """Resistance to a double-click/double-submit: once the first reconnect
    has produced a fresh, live-reachable account, a second call must never
    create a second active account for the same garage."""
    settings = _broken_settings(session, garage, account_id="acct_broken_old")
    _, v2_accounts, _ = _patch_stripe(
        monkeypatch, broken_account_id="acct_broken_old", new_account_id="acct_fresh_1"
    )

    first = platform_client.post(
        f"/api/platform-admin/tenants/{garage.id}/payments/stripe/reconnect",
        json={"reason": "First attempt"},
    )
    second = platform_client.post(
        f"/api/platform-admin/tenants/{garage.id}/payments/stripe/reconnect",
        json={"reason": "Accidental double click"},
    )

    assert first.status_code == 200
    assert second.status_code == 422
    session.refresh(settings)
    assert settings.stripe_account_id == "acct_fresh_1"
    # Only the first call ever reached Stripe's account-creation endpoint.
    assert len(v2_accounts.created) == 1
    assert GaragePaymentAccountHistory.query.filter_by(garage_id=garage.id).count() == 1


def test_reconnect_never_runs_automatically_from_an_ordinary_status_refresh(
    session, garage, monkeypatch
):
    """Reconnect is a deliberate Platform Admin action, never a side effect
    of an ordinary status check - refresh_connect_status only clears the
    cached capability flags (see connect.py's own regression tests) and
    never detaches or replaces the connected account by itself."""
    from app.payments.connect import refresh_connect_status

    settings = _broken_settings(session, garage, account_id="acct_broken_old")
    _patch_stripe(monkeypatch, broken_account_id="acct_broken_old")

    from app.payments.connect import ConnectError

    try:
        refresh_connect_status(garage)
    except ConnectError:
        pass

    session.refresh(settings)
    assert settings.stripe_account_id == "acct_broken_old"
    assert GaragePaymentAccountHistory.query.count() == 0


def test_reconnect_preserves_existing_payment_history_association(
    platform_client, session, garage, monkeypatch
):
    """A booking's payment must keep pointing at the account that actually
    processed it, regardless of what the tenant's *current* account becomes
    afterwards."""
    settings = _broken_settings(session, garage, account_id="acct_broken_old")
    booking = BookingRequest(
        garage_id=garage.id,
        status="PENDING",
        booking_reference="BKRECONNECT",
        customer_first_name="Alex",
        customer_last_name="Turner",
        customer_email="alex@example.com",
        customer_phone="+447123456789",
        vehicle_registration="CN11REC",
        preferred_date=datetime.datetime.now(datetime.UTC).date() + datetime.timedelta(days=7),
        preferred_time=datetime.time(9, 30),
    )
    session.add(booking)
    session.flush()
    payment = BookingPayment(
        garage_id=garage.id,
        booking_request_id=booking.id,
        provider="stripe",
        provider_account_id="acct_broken_old",
        provider_payment_id="pi_before_reconnect",
        amount_minor=2000,
        currency="GBP",
        status="SUCCEEDED",
    )
    session.add(payment)
    session.commit()

    _patch_stripe(monkeypatch, broken_account_id="acct_broken_old", new_account_id="acct_fresh_1")

    response = platform_client.post(
        f"/api/platform-admin/tenants/{garage.id}/payments/stripe/reconnect",
        json={"reason": "Recovering after account loss"},
    )

    assert response.status_code == 200
    session.refresh(payment)
    session.refresh(settings)
    assert payment.provider_account_id == "acct_broken_old"
    assert settings.stripe_account_id == "acct_fresh_1"


def test_reconnect_route_requires_superadmin(support_client, session, garage, monkeypatch):
    _broken_settings(session, garage, account_id="acct_broken_old")
    _patch_stripe(monkeypatch, broken_account_id="acct_broken_old")

    response = support_client.post(
        f"/api/platform-admin/tenants/{garage.id}/payments/stripe/reconnect",
        json={"reason": "Support trying to fix it directly"},
    )

    assert response.status_code == 403
