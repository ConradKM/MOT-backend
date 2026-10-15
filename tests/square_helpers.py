"""An in-process stand-in for Square's REST API, served through
``httpx.MockTransport`` (see app/payments/square_api.py::TRANSPORT_EXTENSION_KEY)
so the real Square adapter/OAuth code runs end to end with no network and no
Square account.

Modelled on Square's documented wire shapes (status strings, error envelope,
idempotency semantics, 45-char idempotency-key limit), not on our own adapter
- so a mapping bug in the adapter fails these tests instead of being mirrored
by them.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx

from app.models.payments.garage_payment_settings import GaragePaymentSettings
from app.payments.secrets import encrypt_token
from app.payments.square_api import TRANSPORT_EXTENSION_KEY

APPLICATION_ID = "sandbox-sq0idb-test-application"
APPLICATION_SECRET = "test-application-secret-not-real"
SIGNATURE_KEY = "test-webhook-signature-key-not-real"
NOTIFICATION_URL = "https://api.comaz.test/api/webhooks/payments/square"
REDIRECT_URL = "https://api.comaz.test/api/payments/square/oauth/callback"


def configure_square(app) -> None:
    app.config.update(
        SQUARE_APPLICATION_ID=APPLICATION_ID,
        SQUARE_APPLICATION_SECRET=APPLICATION_SECRET,
        SQUARE_ENVIRONMENT="sandbox",
        SQUARE_WEBHOOK_SIGNATURE_KEY=SIGNATURE_KEY,
        SQUARE_WEBHOOK_NOTIFICATION_URL=NOTIFICATION_URL,
        SQUARE_OAUTH_REDIRECT_URL=REDIRECT_URL,
    )


def unconfigure_square(app) -> None:
    app.config.update(
        SQUARE_APPLICATION_ID="",
        SQUARE_APPLICATION_SECRET="",
        SQUARE_WEBHOOK_SIGNATURE_KEY="",
        SQUARE_WEBHOOK_NOTIFICATION_URL="",
        SQUARE_OAUTH_REDIRECT_URL="",
    )


def sign(body: bytes, *, url: str = NOTIFICATION_URL, key: str = SIGNATURE_KEY) -> str:
    return base64.b64encode(
        hmac.new(key.encode(), url.encode() + body, hashlib.sha256).digest()
    ).decode()


def _error(
    status: int, code: str, category: str = "INVALID_REQUEST_ERROR", **extra
) -> httpx.Response:
    body = {"errors": [{"category": category, "code": code, "detail": f"{code} detail"}], **extra}
    return httpx.Response(status, json=body)


class FakeSquare:
    """State + request log for one test. Knobs (set before the call):

    - ``decline_next`` - the next CreatePayment fails as CARD_DECLINED
    - ``unauthorized_next`` - the next bearer call returns 401 once
    - ``refresh_result`` - None (ok) / "revoked" (401) / "error" (500)
    - ``payment_status_on_create`` - what CreatePayment returns (COMPLETED)
    """

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.payments: dict[str, dict] = {}
        self.refunds: dict[str, dict] = {}
        self._payment_keys: dict[str, tuple[str, dict]] = {}
        self._refund_keys: dict[str, tuple[str, dict]] = {}
        self.merchant_id = "MLTESTMERCHANT1"
        self.locations = [
            {
                "id": "LOC_GB_1",
                "name": "Main garage",
                "status": "ACTIVE",
                "currency": "GBP",
                "capabilities": ["CREDIT_CARD_PROCESSING", "AUTOMATIC_TRANSFERS"],
            }
        ]
        self.valid_tokens: set[str] = set()
        self.token_counter = 0
        self.refresh_count = 0
        self.revoked = False
        self.decline_next = False
        self.unauthorized_next = False
        self.refresh_result: str | None = None
        self.payment_status_on_create = "COMPLETED"
        self.token_expires_in = timedelta(days=30)

    # --- helpers for tests ---------------------------------------------

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def issue_token(self) -> str:
        self.token_counter += 1
        token = f"EAAA-test-access-{self.token_counter}"
        self.valid_tokens.add(token)
        return token

    def calls(self, method: str, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and r.url.path == path]

    # --- wire ------------------------------------------------------------

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        body = json.loads(request.content) if request.content else {}

        if path == "/oauth2/token":
            return self._token(body)
        if path == "/oauth2/revoke":
            if request.headers.get("Authorization") != f"Client {APPLICATION_SECRET}":
                return httpx.Response(401, json={"message": "bad client", "type": "unauthorized"})
            self.revoked = True
            self.valid_tokens.clear()
            return httpx.Response(200, json={"success": True})

        auth = request.headers.get("Authorization", "")
        if self.unauthorized_next:
            self.unauthorized_next = False
            return _error(401, "UNAUTHORIZED", "AUTHENTICATION_ERROR")
        if not auth.startswith("Bearer ") or auth[7:] not in self.valid_tokens:
            return _error(401, "UNAUTHORIZED", "AUTHENTICATION_ERROR")

        if path == "/v2/locations":
            return httpx.Response(200, json={"locations": self.locations})
        if path == "/v2/merchants/me":
            return httpx.Response(
                200, json={"merchant": {"id": self.merchant_id, "business_name": "Test Motors Ltd"}}
            )
        if path == "/v2/payments" and request.method == "POST":
            return self._create_payment(body)
        if path.startswith("/v2/payments/") and path.endswith("/cancel"):
            payment = self.payments.get(path.split("/")[3])
            if payment is None:
                return _error(404, "NOT_FOUND")
            payment["status"] = "CANCELED"
            return httpx.Response(200, json={"payment": payment})
        if path.startswith("/v2/payments/"):
            payment = self.payments.get(path.split("/")[3])
            return (
                httpx.Response(200, json={"payment": payment})
                if payment
                else _error(404, "NOT_FOUND")
            )
        if path == "/v2/refunds":
            return self._refund(body)
        return _error(404, "NOT_FOUND")

    def _token(self, body: dict) -> httpx.Response:
        if (
            body.get("client_id") != APPLICATION_ID
            or body.get("client_secret") != APPLICATION_SECRET
        ):
            return httpx.Response(401, json={"message": "bad client", "type": "unauthorized"})
        if body.get("grant_type") == "authorization_code":
            if body.get("code") != "good-code":
                return httpx.Response(400, json={"message": "bad code", "type": "bad_request"})
        elif body.get("grant_type") == "refresh_token":
            self.refresh_count += 1
            if self.refresh_result == "revoked":
                return httpx.Response(401, json={"message": "revoked", "type": "unauthorized"})
            if self.refresh_result == "error":
                return httpx.Response(500, json={"message": "oops", "type": "internal"})
            if body.get("refresh_token") != "EQAA-test-refresh":
                return httpx.Response(400, json={"message": "bad refresh", "type": "bad_request"})
        else:
            return httpx.Response(400, json={"message": "bad grant", "type": "bad_request"})
        return httpx.Response(
            200,
            json={
                "access_token": self.issue_token(),
                "token_type": "bearer",
                "expires_at": (datetime.now(UTC) + self.token_expires_in)
                .isoformat()
                .replace("+00:00", "Z"),
                "merchant_id": self.merchant_id,
                "refresh_token": "EQAA-test-refresh",
                "short_lived": False,
            },
        )

    def _create_payment(self, body: dict) -> httpx.Response:
        key = body.get("idempotency_key") or ""
        if not key or len(key) > 45:
            return _error(400, "VALUE_TOO_LONG" if key else "MISSING_REQUIRED_PARAMETER")
        if key in self._payment_keys:
            payment_id, previous = self._payment_keys[key]
            if previous != body:
                return _error(400, "IDEMPOTENCY_KEY_REUSED")
            return httpx.Response(200, json={"payment": self.payments[payment_id]})
        payment_id = f"sq_pay_{uuid.uuid4().hex[:16]}"
        status = "FAILED" if self.decline_next else self.payment_status_on_create
        payment = {
            "id": payment_id,
            "status": status,
            "amount_money": body["amount_money"],
            "location_id": body["location_id"],
            "reference_id": body.get("reference_id"),
            "source_type": "CARD",
        }
        self.payments[payment_id] = payment
        self._payment_keys[key] = (payment_id, body)
        if self.decline_next:
            self.decline_next = False
            return _error(400, "CARD_DECLINED", "PAYMENT_METHOD_ERROR", payment=payment)
        return httpx.Response(200, json={"payment": payment})

    def _refund(self, body: dict) -> httpx.Response:
        key = body.get("idempotency_key") or ""
        if not key or len(key) > 45:
            return _error(400, "VALUE_TOO_LONG")
        if key in self._refund_keys:
            return httpx.Response(200, json={"refund": self.refunds[self._refund_keys[key][0]]})
        payment = self.payments.get(str(body.get("payment_id")))
        if payment is None:
            return _error(404, "NOT_FOUND")
        refund_id = f"sq_ref_{uuid.uuid4().hex[:16]}"
        refund = {
            "id": refund_id,
            "status": "PENDING",
            "amount_money": body["amount_money"],
            "payment_id": payment["id"],
        }
        self.refunds[refund_id] = refund
        self._refund_keys[key] = (refund_id, body)
        return httpx.Response(200, json={"refund": refund})


def install(app, fake: FakeSquare | None = None) -> FakeSquare:
    fake = fake or FakeSquare()
    configure_square(app)
    app.extensions[TRANSPORT_EXTENSION_KEY] = fake.transport()
    return fake


def uninstall(app) -> None:
    app.extensions.pop(TRANSPORT_EXTENSION_KEY, None)
    unconfigure_square(app)


def connect_garage(
    session,
    garage,
    fake: FakeSquare,
    *,
    provider: str = "square",
    location_id: str | None = "LOC_GB_1",
    expires_at: datetime | None = None,
    merchant_id: str | None = None,
) -> GaragePaymentSettings:
    """A garage with a stored, encrypted, CONNECTED Square authorisation -
    what the OAuth callback leaves behind."""
    settings = garage.payment_settings or GaragePaymentSettings(garage_id=garage.id)
    settings.provider = provider
    settings.square_merchant_id = merchant_id or fake.merchant_id
    settings.square_location_id = location_id
    settings.square_access_token_enc = encrypt_token(fake.issue_token())
    settings.square_refresh_token_enc = encrypt_token("EQAA-test-refresh")
    settings.square_token_expires_at = expires_at or datetime.now(UTC) + timedelta(days=30)
    settings.square_connected_at = datetime.now(UTC)
    settings.square_status = "CONNECTED"
    session.add(settings)
    session.commit()
    return settings


def webhook_body(
    event_type: str, obj: dict, *, merchant_id: str, event_id: str | None = None
) -> bytes:
    data_type = event_type.split(".")[0]
    return json.dumps(
        {
            "merchant_id": merchant_id,
            "type": event_type,
            "event_id": event_id or str(uuid.uuid4()),
            "created_at": datetime.now(UTC).isoformat(),
            "data": {"type": data_type, "id": obj.get("id", "x"), "object": {data_type: obj}},
        }
    ).encode()
