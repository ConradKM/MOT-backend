"""Unit tests for the low-level app/email send_email primitive itself - the
provider dispatch and the Resend payload it builds (display name, reply-to).
app/email/service.py's own tests (tests/test_email_service.py) cover the
higher-level template/logging/dedupe layer with send_email mocked out; these
mock one level deeper, at the `resend` SDK call, so the payload Resend
actually receives is what's under test here.
"""

import pytest

from app.email import send_email


class _FakeResendEmails:
    def __init__(self):
        self.calls: list[dict] = []

    def send(self, payload):
        self.calls.append(payload)
        return {"id": "fake-id"}


@pytest.fixture()
def fake_resend(app, monkeypatch):
    import resend

    fake = _FakeResendEmails()
    monkeypatch.setattr(resend, "Emails", fake)
    monkeypatch.setitem(app.config, "EMAIL_PROVIDER", "resend")
    monkeypatch.setitem(app.config, "RESEND_API_KEY", "re_test_key")
    monkeypatch.setitem(app.config, "RESEND_FROM_EMAIL", "bookings@comaz.co.uk")
    return fake


def test_resend_send_uses_display_name_and_reply_to(app, fake_resend):
    send_email(
        to="customer@example.com",
        subject="Your appointment is confirmed",
        body="text body",
        html_body="<p>html body</p>",
        from_name="Kingsway MOT & Service Centre",
        reply_to="owner@kingsway-mot.example",
    )

    assert len(fake_resend.calls) == 1
    payload = fake_resend.calls[0]
    assert payload["from"] == '"Kingsway MOT & Service Centre" <bookings@comaz.co.uk>'
    assert payload["reply_to"] == "owner@kingsway-mot.example"
    assert payload["to"] == ["customer@example.com"]
    assert payload["html"] == "<p>html body</p>"


def test_resend_send_without_name_or_reply_to_omits_them(app, fake_resend):
    send_email(to="customer@example.com", subject="Subject", body="text body")

    payload = fake_resend.calls[0]
    assert payload["from"] == "bookings@comaz.co.uk"
    assert "reply_to" not in payload


def test_resend_send_strips_embedded_quotes_from_the_business_name(app, fake_resend):
    # An embedded `"` would otherwise break the "Display Name <addr>" quoting.
    send_email(
        to="customer@example.com",
        subject="Subject",
        body="text body",
        from_name='Kingsway "Best" MOT',
    )

    payload = fake_resend.calls[0]
    assert payload["from"] == '"Kingsway Best MOT" <bookings@comaz.co.uk>'


def test_resend_raises_without_api_key(app, monkeypatch):
    monkeypatch.setitem(app.config, "EMAIL_PROVIDER", "resend")
    monkeypatch.setitem(app.config, "RESEND_API_KEY", "")

    with pytest.raises(RuntimeError, match="RESEND_API_KEY"):
        send_email(to="customer@example.com", subject="Subject", body="body")
