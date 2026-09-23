"""voice.buy_number's reconciliation: recovering an already-purchased number
after a lost response or a crashed commit, without ever buying a second one.

Uses a fake Twilio client double rather than the real SDK - these exercise
voice.py's own logic directly, one level below the route/service tests in
tests/api/test_platform_admin_communications.py which mock voice.buy_number
itself."""

from __future__ import annotations

import pytest

from app.communications.provisioning import voice


class _FakeNumberResource:
    def __init__(self, phone_number, sid):
        self.phone_number = phone_number
        self.sid = sid
        self.capabilities = {"voice": True}
        self.updated_with = None

    def update(self, **kwargs):
        self.updated_with = kwargs
        return self


class _FakeIncomingPhoneNumbers:
    def __init__(self, existing=None):
        self._existing = existing
        self.create_calls = []
        self.list_calls = []

    def list(self, phone_number=None, limit=None):
        self.list_calls.append(phone_number)
        if self._existing and self._existing.phone_number == phone_number:
            return [self._existing]
        return []

    def create(self, **kwargs):
        self.create_calls.append(kwargs)
        return _FakeNumberResource(kwargs["phone_number"], "PNnew0000000000000000000000000001")


class _FakeClient:
    def __init__(self, existing=None):
        self.incoming_phone_numbers = _FakeIncomingPhoneNumbers(existing)


@pytest.fixture(autouse=True)
def _twilio_configured(app):
    app.config["TWILIO_ACCOUNT_SID"] = "ACmaster0000000000000000000000001"
    app.config["TWILIO_AUTH_TOKEN"] = "token"
    app.config["PUBLIC_API_BASE_URL"] = "https://example.onrender.com"
    yield


def test_buy_number_purchases_when_nothing_already_owned(monkeypatch, garage):
    client = _FakeClient()
    monkeypatch.setattr(voice, "get_client_for_subaccount_resources", lambda g: client)

    result = voice.buy_number(garage, "+441234567890")

    assert len(client.incoming_phone_numbers.create_calls) == 1
    assert result["sid"] == "PNnew0000000000000000000000000001"


def test_buy_number_reconciles_an_already_owned_number_instead_of_buying_again(monkeypatch, garage):
    """The exact residual risk this closes: Twilio's purchase succeeded on a
    prior attempt, but CoMaz never recorded it (lost response, crashed
    commit). A retry must recover that number, not buy a second one."""
    already_owned = _FakeNumberResource("+441234567890", "PNexisting00000000000000000000001")
    client = _FakeClient(existing=already_owned)
    monkeypatch.setattr(voice, "get_client_for_subaccount_resources", lambda g: client)

    result = voice.buy_number(garage, "+441234567890")

    assert client.incoming_phone_numbers.create_calls == []  # never bought a second number
    assert result["sid"] == "PNexisting00000000000000000000001"
    assert already_owned.updated_with["voice_method"] == "POST"


def test_buy_number_never_confuses_a_different_number_for_an_existing_purchase(monkeypatch, garage):
    other_number = _FakeNumberResource("+449999999999", "PNother00000000000000000000000001")
    client = _FakeClient(existing=other_number)
    monkeypatch.setattr(voice, "get_client_for_subaccount_resources", lambda g: client)

    result = voice.buy_number(garage, "+441234567890")

    assert len(client.incoming_phone_numbers.create_calls) == 1
    assert result["sid"] == "PNnew0000000000000000000000000001"
