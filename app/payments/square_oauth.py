"""Square OAuth - one authorised Square merchant per business, the Square
analogue of Stripe Connect (app/payments/connect.py).

CoMaz registers ONE Square application (``SQUARE_APPLICATION_ID``/``_SECRET``);
each business authorises it on Square's own page and CoMaz stores *that
merchant's* tokens, Fernet-encrypted (app/payments/secrets.py). Deposits are
then charged with the merchant's own token, so funds settle straight to the
business - CoMaz never holds the money and takes no application fee.

Flow:

1. ``POST /api/payments/square/connect`` (owner) -> :func:`authorize_url`: the
   Square authorise URL with a signed, expiring ``state`` bound to the garage
   and the employee who started it (CSRF protection).
2. Square redirects the browser to the backend callback
   (``SQUARE_OAUTH_REDIRECT_URL``) -> :func:`complete_authorization`:
   validates ``state``, exchanges ``code`` for tokens, encrypts and stores
   them, fetches the merchant's locations, then the route redirects to
   ``/<garageId>/settings/payments?square=return``. The frontend never trusts
   that query string - it re-reads ``GET /square/status``.

Token lifecycle: Square access tokens last about 30 days. They are refreshed
lazily, at point of use (:func:`access_token_for_merchant`), under a row lock,
with one forced refresh on a 401 from the adapter - this repo has no scheduler
of its own (app/tasks/config.py), so nothing may depend on a periodic job.
``flask refresh-square-tokens`` exists to catch stragglers by hand or from an
external cron. A revoked or unrefreshable authorisation flips
``square_status`` to REVOKED/ERROR, which stops deposits for that business
(app/payments/settings.py::provider_account_ready) and shows in Settings and
Platform Admin.

Never logs, returns, audits or raises with a token in it.
"""

from __future__ import annotations

import secrets as _stdlib_secrets
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from urllib.parse import urlencode

from flask import current_app
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.extensions import db
from app.models.garage import Garage
from app.models.payments.garage_payment_settings import (
    SQUARE_STATUS_CONNECTED,
    SQUARE_STATUS_ERROR,
    SQUARE_STATUS_NOT_CONNECTED,
    SQUARE_STATUS_REVOKED,
    GaragePaymentSettings,
)
from app.payments import square_api
from app.payments.audit import record_payment_event
from app.payments.secrets import (
    PaymentsSecretDecryptionError,
    PaymentsSecretsNotConfiguredError,
    decrypt_token,
    encrypt_token,
    payments_secrets_configured,
)

#: Minimal scopes: CreatePayment/RefundPayment need PAYMENTS_WRITE,
#: GetPayment PAYMENTS_READ, and ListLocations/RetrieveMerchant
#: MERCHANT_PROFILE_READ. Nothing else (no customers, orders, or catalog).
SQUARE_OAUTH_SCOPES = ("PAYMENTS_WRITE", "PAYMENTS_READ", "MERCHANT_PROFILE_READ")

#: How long a ``state`` stays valid - long enough to sign in to Square and
#: approve, short enough that a leaked URL is useless soon after.
STATE_MAX_AGE_SECONDS = 15 * 60
_STATE_SALT = "comaz-square-oauth-state"

#: Square recommends refreshing at least every 7 days; refreshing once a token
#: is inside this margin keeps a merchant that takes deposits rarely from ever
#: hitting a hard expiry.
REFRESH_MARGIN = timedelta(days=7)

#: Square location capability that means "can take card payments".
_CARD_CAPABILITY = "CREDIT_CARD_PROCESSING"


class SquareConnectError(RuntimeError):
    """A Square connection step failed or was attempted before it could
    succeed. ``code`` is a stable, safe identifier for the route/UI."""

    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code


class SquareNotConnected(SquareConnectError):
    """No usable Square authorisation for this merchant right now
    (never connected, disconnected, revoked, or the token can't be used)."""


# --- deployment prerequisites ----------------------------------------------


def square_prerequisites() -> list[tuple[str, str, bool, str]]:
    """``(key, label, satisfied, how_to_fix)`` for every deployment-level
    Square setting - reported by app/payments/config.py::payments_prerequisites
    so Platform Admin can see exactly what's missing, like Stripe's."""
    cfg = current_app.config
    env = str(cfg.get("SQUARE_ENVIRONMENT") or "").lower()
    return [
        (
            "square_application_id",
            "Square application ID",
            bool(cfg.get("SQUARE_APPLICATION_ID")),
            "Set SQUARE_APPLICATION_ID from the Square Developer Console (Credentials).",
        ),
        (
            "square_application_secret",
            "Square application secret",
            bool(cfg.get("SQUARE_APPLICATION_SECRET")),
            "Set SQUARE_APPLICATION_SECRET from the Square Developer Console (OAuth).",
        ),
        (
            "square_environment",
            "Square environment",
            env in ("sandbox", "production"),
            "Set SQUARE_ENVIRONMENT to 'sandbox' or 'production'.",
        ),
        (
            "square_oauth_redirect_url",
            "Square OAuth redirect URL",
            bool(cfg.get("SQUARE_OAUTH_REDIRECT_URL")),
            (
                "Set SQUARE_OAUTH_REDIRECT_URL to this API's "
                "/api/payments/square/oauth/callback URL and register the same URL "
                "as the application's OAuth Redirect URL in the Square Developer Console."
            ),
        ),
        (
            "square_webhook_signature_key",
            "Square webhook signature key",
            bool(cfg.get("SQUARE_WEBHOOK_SIGNATURE_KEY")),
            (
                "Create a webhook subscription in the Square Developer Console and set "
                "SQUARE_WEBHOOK_SIGNATURE_KEY to its signature key."
            ),
        ),
        (
            "square_webhook_notification_url",
            "Square webhook notification URL",
            bool(cfg.get("SQUARE_WEBHOOK_NOTIFICATION_URL")),
            (
                "Set SQUARE_WEBHOOK_NOTIFICATION_URL to the exact notification URL "
                "registered with Square (…/api/webhooks/payments/square)."
            ),
        ),
        (
            "payments_secret_key",
            "Payments token encryption key",
            payments_secrets_configured(),
            (
                "Set PAYMENTS_SECRET_KEY to a Fernet key (separate from COMMS_SECRET_KEY) "
                "so connected Square merchants' tokens can be stored encrypted."
            ),
        ),
    ]


def square_deployment_configured() -> bool:
    return all(ok for _key, _label, ok, _fix in square_prerequisites())


# --- per-business readiness -------------------------------------------------


def square_account_ready(settings: GaragePaymentSettings | None) -> bool:
    """Whether a business's Square connection can take a deposit right now:
    connected, a location chosen, and a token that is either still valid or
    refreshable. Read from stored state only - no network call - so it is
    cheap enough for readiness dashboards and per-request gating."""
    if settings is None:
        return False
    if (
        settings.square_status != SQUARE_STATUS_CONNECTED
        or not settings.square_merchant_id
        or not settings.square_location_id
        or not settings.square_access_token_enc
    ):
        return False
    expires_at = settings.square_token_expires_at
    if expires_at is not None and expires_at <= datetime.now(UTC):
        return bool(settings.square_refresh_token_enc)
    return True


def settings_for_merchant(merchant_id: str | None) -> GaragePaymentSettings | None:
    """Exact-match tenant resolution for a Square merchant id (webhooks,
    refunds). Never a fuzzy/prefix match."""
    if not merchant_id:
        return None
    return cast(
        GaragePaymentSettings | None,
        GaragePaymentSettings.query.filter_by(square_merchant_id=merchant_id).first(),
    )


# --- OAuth start / callback -------------------------------------------------


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt=_STATE_SALT)


def make_state(garage_id, employee_id) -> str:
    return str(
        _serializer().dumps(
            {
                "g": str(garage_id),
                "e": str(employee_id),
                "n": _stdlib_secrets.token_urlsafe(16),
            }
        )
    )


def read_state(state: str | None) -> dict:
    """The verified ``state`` payload, or :class:`SquareConnectError` for a
    missing, tampered or expired one."""
    if not state:
        raise SquareConnectError("Missing OAuth state.", code="invalid_state")
    try:
        data = _serializer().loads(state, max_age=STATE_MAX_AGE_SECONDS)
    except SignatureExpired as exc:
        raise SquareConnectError(
            "The Square connection link expired.", code="state_expired"
        ) from exc
    except BadSignature as exc:
        raise SquareConnectError("Invalid OAuth state.", code="invalid_state") from exc
    if not isinstance(data, dict) or not data.get("g") or not data.get("e"):
        raise SquareConnectError("Invalid OAuth state.", code="invalid_state")
    return data


def _require_deployment_configured() -> None:
    cfg = current_app.config
    if not (
        cfg.get("SQUARE_APPLICATION_ID")
        and cfg.get("SQUARE_APPLICATION_SECRET")
        and cfg.get("SQUARE_OAUTH_REDIRECT_URL")
    ):
        raise SquareConnectError(
            "Square is not configured for this deployment.", code="not_configured"
        )
    if not payments_secrets_configured():
        # Checked before sending anyone to Square: a successful authorisation
        # we then couldn't store would leave a dangling grant in Square.
        raise SquareConnectError(
            "Square tokens cannot be stored securely on this deployment yet "
            "(PAYMENTS_SECRET_KEY is not set).",
            code="secrets_not_configured",
        )


def authorize_url(garage: Garage, employee) -> str:
    """Square's own authorisation page for this business, carrying a signed
    state bound to ``garage`` and ``employee``."""
    _require_deployment_configured()
    cfg = current_app.config
    query = {
        "client_id": cfg["SQUARE_APPLICATION_ID"],
        "scope": " ".join(SQUARE_OAUTH_SCOPES),
        # Production requires session=false (always show the seller sign-in
        # rather than silently using an existing Square Dashboard session);
        # Square ignores it in sandbox.
        "session": "false",
        "state": make_state(garage.id, employee.id),
        "redirect_uri": cfg["SQUARE_OAUTH_REDIRECT_URL"],
    }
    return f"{square_api.base_url()}/oauth2/authorize?{urlencode(query)}"


def settings_return_url(garage_id, *, error: str | None = None) -> str:
    base = current_app.config["APP_BASE_URL"].rstrip("/")
    url = f"{base}/{garage_id}/settings/payments?square=return"
    if error:
        # A stable code only (never Square's raw text) - informational; the
        # page still re-reads /square/status as the source of truth.
        url += f"&square_error={error}"
    return url


def _parse_expiry(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _get_or_create_settings(garage: Garage) -> GaragePaymentSettings:
    settings = (
        db.session.query(GaragePaymentSettings)
        .filter_by(garage_id=garage.id)
        .with_for_update()
        .one_or_none()
    )
    if settings is None:
        from app.payments.settings import resolve_provider_name

        # Connecting Square never silently switches the active provider -
        # that's the owner's explicit choice (PUT /api/payments/provider).
        settings = GaragePaymentSettings(
            garage_id=garage.id, provider=resolve_provider_name(garage)
        )
        db.session.add(settings)
        db.session.flush()
    return settings


def _validate_state_owner(state: dict) -> tuple[Garage, Any]:
    from app.models.employee import Employee

    try:
        import uuid

        garage_id = uuid.UUID(state["g"])
        employee_id = uuid.UUID(state["e"])
    except (ValueError, KeyError) as exc:
        raise SquareConnectError("Invalid OAuth state.", code="invalid_state") from exc
    garage = db.session.get(Garage, garage_id)
    employee = db.session.get(Employee, employee_id)
    # The employee must still belong to the garage the state names and still
    # be its owner - a state minted for one business can never connect a
    # merchant to another.
    if (
        garage is None
        or employee is None
        or employee.garage_id != garage.id
        or not employee.has_role("OWNER")
    ):
        raise SquareConnectError("Invalid OAuth state.", code="wrong_garage")
    return garage, employee


def complete_authorization(*, state: str | None, code: str | None, error: str | None) -> Garage:
    """Handle Square's redirect back. Returns the garage on success; raises
    :class:`SquareConnectError` (with ``garage`` attached when the state
    itself was valid, so the route can still send the owner back to their
    own settings page)."""
    payload = read_state(state)
    garage, employee = _validate_state_owner(payload)

    if error or not code:
        # access_denied when the seller clicks Deny, or any other OAuth error.
        exc = SquareConnectError("Square authorisation was not completed.", code="denied")
        exc.garage = garage  # type: ignore[attr-defined]
        raise exc

    try:
        _require_deployment_configured()
        tokens = _obtain_token(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": current_app.config["SQUARE_OAUTH_REDIRECT_URL"],
            }
        )
        merchant_id = tokens.get("merchant_id")
        access_token = tokens.get("access_token")
        if not merchant_id or not access_token:
            raise SquareConnectError("Square returned an incomplete token.", code="token_exchange")

        existing = settings_for_merchant(merchant_id)
        if existing is not None and existing.garage_id != garage.id:
            # One Square merchant, one CoMaz business - webhooks resolve the
            # tenant by merchant id, so sharing would be ambiguous.
            raise SquareConnectError(
                "This Square account is already connected to another business.",
                code="merchant_in_use",
            )

        settings = _get_or_create_settings(garage)
        settings.square_merchant_id = merchant_id
        settings.square_access_token_enc = encrypt_token(access_token)
        refresh_token = tokens.get("refresh_token")
        settings.square_refresh_token_enc = encrypt_token(refresh_token) if refresh_token else None
        settings.square_token_expires_at = _parse_expiry(tokens.get("expires_at"))
        settings.square_connected_at = datetime.now(UTC)
        settings.square_status = SQUARE_STATUS_CONNECTED

        # Auto-select the location when there's exactly one eligible; keep a
        # previous choice if it's still eligible (a reconnect).
        try:
            eligible = [loc for loc in list_locations(access_token) if loc["eligible"]]
        except SquareConnectError:
            eligible = []
        eligible_ids = {loc["id"] for loc in eligible}
        if settings.square_location_id not in eligible_ids:
            settings.square_location_id = eligible[0]["id"] if len(eligible) == 1 else None

        record_payment_event(
            garage_id=garage.id,
            action="payment.square.connected",
            summary="Square account connected.",
            details={"merchant_id": merchant_id, "employee_id": str(employee.id)},
        )
        db.session.commit()
    except SquareConnectError as exc:
        db.session.rollback()
        exc.garage = garage  # type: ignore[attr-defined]
        raise
    except PaymentsSecretsNotConfiguredError as exc:
        db.session.rollback()
        wrapped = SquareConnectError(str(exc), code="secrets_not_configured")
        wrapped.garage = garage  # type: ignore[attr-defined]
        raise wrapped from exc
    return garage


def _obtain_token(grant: dict) -> dict:
    cfg = current_app.config
    body = {
        "client_id": cfg["SQUARE_APPLICATION_ID"],
        "client_secret": cfg["SQUARE_APPLICATION_SECRET"],
        **grant,
    }
    try:
        return square_api.request("POST", "/oauth2/token", json=body)
    except square_api.SquareApiError as exc:
        raise SquareConnectError(
            "Square rejected the token request.",
            code="revoked" if exc.status == 401 else "token_exchange",
        ) from exc


# --- locations / status -----------------------------------------------------


def _deposit_currency() -> str:
    return str(current_app.config.get("DEPOSIT_CURRENCY", "GBP"))


def list_locations(access_token: str) -> list[dict]:
    """The merchant's locations, each marked ``eligible`` when it can take
    card payments in the business's deposit currency."""
    try:
        body = square_api.request("GET", "/v2/locations", bearer_token=access_token)
    except square_api.SquareApiError as exc:
        raise SquareConnectError("Could not load Square locations.", code="locations") from exc
    currency = _deposit_currency()
    locations = []
    for loc in body.get("locations") or []:
        capabilities = loc.get("capabilities") or []
        locations.append(
            {
                "id": loc.get("id"),
                "name": loc.get("name") or loc.get("business_name") or loc.get("id"),
                "currency": loc.get("currency"),
                "can_take_card_payments": _CARD_CAPABILITY in capabilities,
                "eligible": (
                    loc.get("status") == "ACTIVE"
                    and _CARD_CAPABILITY in capabilities
                    and loc.get("currency") == currency
                ),
            }
        )
    return locations


def _merchant_name(access_token: str) -> str | None:
    try:
        body = square_api.request("GET", "/v2/merchants/me", bearer_token=access_token)
    except square_api.SquareApiError:
        return None
    merchant = body.get("merchant") or {}
    return merchant.get("business_name")


def connection_status(garage: Garage) -> dict:
    """What Settings -> Payments shows for Square. Uses a live merchant/
    locations read when connected (falling back to stored state on any
    failure - never a 500 for a status badge). Never contains a token."""
    settings = garage.payment_settings
    status = settings.square_status if settings is not None else SQUARE_STATUS_NOT_CONNECTED
    result: dict[str, Any] = {
        "status": status,
        "merchant_id": settings.square_merchant_id if settings else None,
        "merchant_name": None,
        "location_id": settings.square_location_id if settings else None,
        "location_name": None,
        "locations": [],
        "can_take_card_payments": False,
        "currency": _deposit_currency(),
        "charges_in_currency": False,
        "ready": square_account_ready(settings),
        "environment": square_api.environment(),
        "deployment_configured": square_deployment_configured(),
    }
    if settings is None or status != SQUARE_STATUS_CONNECTED or not settings.square_merchant_id:
        return result

    try:
        token, settings = access_token_for_merchant(settings.square_merchant_id)
        locations = list_locations(token)
        result["merchant_name"] = _merchant_name(token)
    except SquareConnectError:
        db.session.commit()  # persist any REVOKED/ERROR the refresh recorded
        result["status"] = settings.square_status
        result["ready"] = square_account_ready(settings)
        return result

    db.session.commit()  # persist a lazy refresh, if one happened
    result["locations"] = [
        {k: loc[k] for k in ("id", "name", "currency", "can_take_card_payments", "eligible")}
        for loc in locations
    ]
    chosen = next((loc for loc in locations if loc["id"] == settings.square_location_id), None)
    if chosen is not None:
        result["location_name"] = chosen["name"]
        result["can_take_card_payments"] = chosen["can_take_card_payments"]
        result["charges_in_currency"] = chosen["currency"] == _deposit_currency()
    return result


def set_location(garage: Garage, location_id: str) -> None:
    settings = garage.payment_settings
    if settings is None or settings.square_status != SQUARE_STATUS_CONNECTED:
        raise SquareNotConnected("Square is not connected.", code="not_connected")
    token, settings = access_token_for_merchant(settings.square_merchant_id)
    eligible = {loc["id"] for loc in list_locations(token) if loc["eligible"]}
    if location_id not in eligible:
        raise SquareConnectError(
            f"That location can't take card payments in {_deposit_currency()}.",
            code="location_ineligible",
        )
    settings.square_location_id = location_id
    record_payment_event(
        garage_id=garage.id,
        action="payment.square.location_selected",
        summary="Square location selected.",
        details={"location_id": location_id},
    )
    db.session.commit()


def disconnect(garage: Garage) -> bool:
    """Revoke CoMaz's authorisation with Square and clear local state.
    Returns whether Square confirmed the revoke; local state is cleared
    either way (a failed remote revoke is logged - the owner can also revoke
    from their Square Dashboard)."""
    settings = (
        db.session.query(GaragePaymentSettings)
        .filter_by(garage_id=garage.id)
        .with_for_update()
        .one_or_none()
    )
    if settings is None or not settings.square_merchant_id:
        raise SquareNotConnected("Square is not connected.", code="not_connected")

    merchant_id = settings.square_merchant_id
    revoked = False
    cfg = current_app.config
    if cfg.get("SQUARE_APPLICATION_ID") and cfg.get("SQUARE_APPLICATION_SECRET"):
        try:
            square_api.request(
                "POST",
                "/oauth2/revoke",
                client_secret=cfg["SQUARE_APPLICATION_SECRET"],
                json={"client_id": cfg["SQUARE_APPLICATION_ID"], "merchant_id": merchant_id},
            )
            revoked = True
        except square_api.SquareApiError as exc:
            current_app.logger.warning(
                "SQUARE_OAUTH_REVOKE_FAILED garage=%s merchant=%s status=%s code=%s",
                garage.id,
                merchant_id,
                exc.status,
                exc.code,
            )

    _clear_connection(settings, status=SQUARE_STATUS_NOT_CONNECTED, keep_merchant=False)
    record_payment_event(
        garage_id=garage.id,
        action="payment.square.disconnected",
        summary="Square account disconnected."
        + ("" if revoked else " Square did not confirm the revoke."),
        details={"merchant_id": merchant_id, "remote_revoke_confirmed": revoked},
    )
    db.session.commit()
    return revoked


def _clear_connection(settings: GaragePaymentSettings, *, status: str, keep_merchant: bool) -> None:
    settings.square_access_token_enc = None
    settings.square_refresh_token_enc = None
    settings.square_token_expires_at = None
    settings.square_status = status
    if not keep_merchant:
        settings.square_merchant_id = None
        settings.square_location_id = None
        settings.square_connected_at = None


def mark_revoked(merchant_id: str | None) -> bool:
    """A verified ``oauth.authorization.revoked`` for ``merchant_id`` (the
    analogue of connect.py::sync_account_from_webhook). Keeps the merchant id
    so Settings can show "Needs attention" and a reconnect lands on the same
    row; drops the now-useless tokens. Returns False for an unknown merchant."""
    settings = (
        GaragePaymentSettings.query.filter_by(square_merchant_id=merchant_id)
        .with_for_update()
        .first()
        if merchant_id
        else None
    )
    if settings is None:
        return False
    _clear_connection(settings, status=SQUARE_STATUS_REVOKED, keep_merchant=True)
    current_app.logger.warning(
        "SQUARE_AUTHORIZATION_REVOKED garage=%s merchant=%s", settings.garage_id, merchant_id
    )
    record_payment_event(
        garage_id=settings.garage_id,
        action="payment.square.revoked",
        summary="The business revoked CoMaz's access in Square; online deposits are "
        "paused until Square is reconnected.",
        details={"merchant_id": merchant_id},
    )
    return True


# --- token lifecycle --------------------------------------------------------


def _needs_refresh(settings: GaragePaymentSettings, now: datetime) -> bool:
    expires_at = settings.square_token_expires_at
    return expires_at is not None and expires_at - now <= REFRESH_MARGIN


def access_token_for_merchant(
    merchant_id: str | None, *, force_refresh: bool = False
) -> tuple[str, GaragePaymentSettings]:
    """The decrypted access token for ``merchant_id`` (plus its settings
    row), refreshing first if it's near expiry or ``force_refresh`` (the
    adapter's one retry after a 401).

    The common path reads without a lock. A refresh re-reads the row
    ``FOR UPDATE`` and re-checks, so concurrent callers serialise and only
    the first actually calls Square; the rest see its fresh token. Changes
    are flushed, not committed - the caller's own transaction commits them
    (and every caller commits on success *and* failure paths).
    """
    settings = settings_for_merchant(merchant_id)
    if settings is None:
        raise SquareNotConnected(
            "This Square account is not connected to CoMaz.", code="not_connected"
        )
    now = datetime.now(UTC)
    if settings.square_status == SQUARE_STATUS_CONNECTED and not (
        force_refresh or _needs_refresh(settings, now)
    ):
        return _decrypt_access(settings), settings

    # Captured before the locked re-read, which refreshes this same identity-
    # map object in place.
    seen_expiry = settings.square_token_expires_at
    locked = (
        GaragePaymentSettings.query.filter_by(id=settings.id)
        .with_for_update()
        .populate_existing()
        .one()
    )
    if locked.square_status != SQUARE_STATUS_CONNECTED or not locked.square_access_token_enc:
        raise SquareNotConnected(
            "Square needs to be reconnected for this business.", code=locked.square_status.lower()
        )
    # Another caller may have refreshed while we waited for the lock.
    if not _needs_refresh(locked, datetime.now(UTC)) and not (
        force_refresh and locked.square_token_expires_at == seen_expiry
    ):
        return _decrypt_access(locked), locked
    _refresh(locked)
    return _decrypt_access(locked), locked


def _decrypt_access(settings: GaragePaymentSettings) -> str:
    if not settings.square_access_token_enc:
        raise SquareNotConnected("Square is not connected.", code="not_connected")
    try:
        return decrypt_token(settings.square_access_token_enc)
    except (PaymentsSecretDecryptionError, PaymentsSecretsNotConfiguredError) as exc:
        settings.square_status = SQUARE_STATUS_ERROR
        db.session.flush()
        current_app.logger.error(
            "SQUARE_TOKEN_DECRYPT_FAILED garage=%s merchant=%s",
            settings.garage_id,
            settings.square_merchant_id,
        )
        raise SquareNotConnected(
            "The stored Square credential can't be read; reconnect Square.", code="error"
        ) from exc


def _refresh(settings: GaragePaymentSettings) -> None:
    """Exchange the stored refresh token for a new access token, on an
    already-locked row."""
    if not settings.square_refresh_token_enc:
        _mark_unusable(settings, SQUARE_STATUS_ERROR, "no_refresh_token")
        raise SquareNotConnected("Square needs to be reconnected.", code="error")
    try:
        refresh_token = decrypt_token(settings.square_refresh_token_enc)
        tokens = _obtain_token({"grant_type": "refresh_token", "refresh_token": refresh_token})
    except (PaymentsSecretDecryptionError, PaymentsSecretsNotConfiguredError) as exc:
        _mark_unusable(settings, SQUARE_STATUS_ERROR, "decrypt_failed")
        raise SquareNotConnected("Square needs to be reconnected.", code="error") from exc
    except SquareConnectError as exc:
        expired = (
            settings.square_token_expires_at is not None
            and settings.square_token_expires_at <= datetime.now(UTC)
        )
        if exc.code == "revoked":
            _mark_unusable(settings, SQUARE_STATUS_REVOKED, "refresh_rejected")
            raise SquareNotConnected("Square access was revoked.", code="revoked") from exc
        if expired:
            _mark_unusable(settings, SQUARE_STATUS_ERROR, "refresh_failed")
            raise SquareNotConnected("Square needs to be reconnected.", code="error") from exc
        # Refresh hiccup but the current token is still valid - keep using
        # it; the next call retries the refresh.
        current_app.logger.warning(
            "SQUARE_TOKEN_REFRESH_DEFERRED garage=%s merchant=%s",
            settings.garage_id,
            settings.square_merchant_id,
        )
        return

    access_token = tokens.get("access_token")
    if not access_token:
        _mark_unusable(settings, SQUARE_STATUS_ERROR, "refresh_incomplete")
        raise SquareNotConnected("Square needs to be reconnected.", code="error")
    settings.square_access_token_enc = encrypt_token(access_token)
    # Code-flow refresh tokens are reusable and Square returns the same one;
    # store whatever it returns in case that ever changes.
    if tokens.get("refresh_token"):
        settings.square_refresh_token_enc = encrypt_token(tokens["refresh_token"])
    settings.square_token_expires_at = _parse_expiry(tokens.get("expires_at"))
    db.session.flush()


def _mark_unusable(settings: GaragePaymentSettings, status: str, reason: str) -> None:
    settings.square_status = status
    db.session.flush()
    current_app.logger.warning(
        "SQUARE_TOKEN_UNUSABLE garage=%s merchant=%s status=%s reason=%s",
        settings.garage_id,
        settings.square_merchant_id,
        status,
        reason,
    )
    record_payment_event(
        garage_id=settings.garage_id,
        action="payment.square.needs_attention",
        summary="Square needs to be reconnected; online deposits are paused.",
        details={"status": status, "reason": reason},
    )


def refresh_stragglers(now: datetime | None = None) -> list[tuple[str, str]]:
    """Refresh every connected merchant whose token is inside the refresh
    margin - ``flask refresh-square-tokens``. Returns ``(garage_id, outcome)``."""
    now = now or datetime.now(UTC)
    outcomes = []
    rows = GaragePaymentSettings.query.filter(
        GaragePaymentSettings.square_status == SQUARE_STATUS_CONNECTED,
        GaragePaymentSettings.square_token_expires_at.isnot(None),
        GaragePaymentSettings.square_token_expires_at <= now + REFRESH_MARGIN,
    ).all()
    for settings in rows:
        try:
            access_token_for_merchant(settings.square_merchant_id)
            outcome = "refreshed"
        except SquareConnectError as exc:
            outcome = f"failed ({exc.code})"
        db.session.commit()
        outcomes.append((str(settings.garage_id), outcome))
    return outcomes
