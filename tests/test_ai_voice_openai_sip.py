"""app/ai_voice/openai_sip.py::verify_webhook against the real openai SDK
(not mocked) - the one thing tests/api/test_ai_voice_routes.py deliberately
doesn't cover, since it monkeypatches verify_webhook itself.

Exists because production hit exactly this gap: a delivery missing the
required signature headers made the SDK raise a bare ``ValueError``, not its
own ``InvalidWebhookSignatureError`` - and the webhook route only ever
caught the latter, so the request 500'd instead of cleanly rejecting with
400. See app/ai_voice/openai_sip.py::verify_webhook's own except clause.
"""

import pytest
from openai import InvalidWebhookSignatureError

from app.ai_voice.openai_sip import verify_webhook


@pytest.fixture(autouse=True)
def _configured(app, monkeypatch):
    monkeypatch.setitem(app.config, "OPENAI_API_KEY", "sk-test-123")
    monkeypatch.setitem(app.config, "OPENAI_WEBHOOK_SECRET", "whsec_dGVzdF9zZWNyZXQ=")


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
