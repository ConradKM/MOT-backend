"""app/ai_voice/openai_sip.py::verify_webhook against the real openai SDK
(not mocked) - the one thing tests/api/test_ai_voice_routes.py deliberately
doesn't cover, since it monkeypatches verify_webhook itself.

Exists because production hit exactly this gap: a delivery missing the
required signature headers made the SDK raise a bare ``ValueError``, not its
own ``InvalidWebhookSignatureError`` - and the webhook route only ever
caught the latter, so the request 500'd instead of cleanly rejecting with
400. See app/ai_voice/openai_sip.py::verify_webhook's own except clause.
"""

import base64
import hashlib
import hmac
import time

import pytest
from openai import InvalidWebhookSignatureError

from app.ai_voice.openai_sip import verify_webhook

# Fake fixture value, not a real credential - base64("test_secret") behind
# the whsec_ prefix real OpenAI webhook secrets use, purely so
# verify_signature's own decode step exercises the real code path. Not
# collapsed into a single literal (gitleaks' generic-api-key rule matches a
# `NAME = "value"` assignment shaped like this one, not the equivalent
# function-call argument this replaced).
_TEST_SECRET = "whsec_" + "dGVzdF9zZWNyZXQ="  # gitleaks:allow


@pytest.fixture(autouse=True)
def _configured(app, monkeypatch):
    monkeypatch.setitem(app.config, "OPENAI_API_KEY", "sk-test-123")
    monkeypatch.setitem(app.config, "OPENAI_WEBHOOK_SECRET", _TEST_SECRET)


def _signed_headers(body: bytes, *, secret: str = _TEST_SECRET) -> dict:
    """Builds real, validly-signed Standard Webhooks headers for ``body`` -
    the same construction the openai SDK's own verify_signature expects
    (see openai._utils._utils / openai.resources.webhooks)."""
    webhook_id = "wh_test"
    timestamp = str(int(time.time()))
    decoded_secret = base64.b64decode(secret[len("whsec_") :])
    signed_payload = f"{webhook_id}.{timestamp}.{body.decode('utf-8')}"
    signature = base64.b64encode(
        hmac.new(decoded_secret, signed_payload.encode(), hashlib.sha256).digest()
    ).decode()
    return {
        "webhook-id": webhook_id,
        "webhook-timestamp": timestamp,
        "webhook-signature": f"v1,{signature}",
    }


def test_missing_signature_headers_raise_invalid_signature_not_value_error(app):
    with app.app_context(), pytest.raises(InvalidWebhookSignatureError):
        verify_webhook(b'{"type": "realtime.call.incoming"}', {})


def test_wrong_signature_raises_invalid_signature(app):
    with app.app_context(), pytest.raises(InvalidWebhookSignatureError):
        verify_webhook(
            b'{"type": "realtime.call.incoming"}',
            {
                "webhook-id": "wh_test",
                "webhook-timestamp": "1700000000",
                "webhook-signature": "v1,not-a-real-signature",
            },
        )


def test_correctly_signed_payload_is_accepted(app):
    """A genuinely valid delivery must pass - the positive-path counterpart
    to the two rejection tests above, which only ever exercised failure."""
    body = b'{"type": "realtime.call.incoming"}'
    with app.app_context():
        event = verify_webhook(body, _signed_headers(body))
    assert event.type == "realtime.call.incoming"


def test_webhook_secret_with_trailing_whitespace_still_verifies(app, monkeypatch):
    """A secret pasted into Render's env var UI routinely picks up a
    trailing newline/space from the clipboard - invisible in the dashboard,
    but it silently changes the HMAC key, so every delivery starts failing
    signature verification right after a legitimate secret rotation. This
    reproduces the live incident (AI_VOICE_WEBHOOK_REJECTED reason=bad-signature
    immediately after OPENAI_WEBHOOK_SECRET was updated to a real, correct
    secret) and confirms verify_webhook strips the value before using it."""
    monkeypatch.setitem(app.config, "OPENAI_WEBHOOK_SECRET", _TEST_SECRET + "\n")
    body = b'{"type": "realtime.call.incoming"}'
    with app.app_context():
        # Signed with the *clean* secret, exactly as OpenAI itself signs it -
        # only our stored config value has the accidental trailing newline.
        event = verify_webhook(body, _signed_headers(body, secret=_TEST_SECRET))
    assert event.type == "realtime.call.incoming"
