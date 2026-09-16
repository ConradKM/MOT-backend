"""Provider boundary tests; no external communications accounts or traffic."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from flask import Flask

from app.communications import service
from app.communications.providers import (
    get_messaging_provider,
    get_sms_provider,
    get_voice_provider,
    provider_name,
)
from app.communications.providers.base import (
    Capabilities,
    ProviderNotConfigured,
    SendResult,
    UnsupportedCapability,
)
from app.communications.providers.sip import SipVoiceProvider
from app.communications.providers.twilio import (
    TwilioMessagingProvider,
    TwilioSMSProvider,
    TwilioVoiceProvider,
)


@pytest.fixture()
def context():
    app = Flask(__name__)
    with app.app_context():
        yield app


@pytest.fixture()
def tenant():
    return SimpleNamespace(
        id="tenant-a",
        communication_settings=SimpleNamespace(
            communications_enabled=True,
            voice_phone_number="+441234567890",
            whatsapp_sender="whatsapp:+14155238886",
            messaging_service_sid=None,
        ),
    )


def test_channel_defaults_and_independent_tenant_selection(context, tenant):
    assert get_voice_provider(tenant).name == "twilio"
    assert get_messaging_provider(tenant).name == "twilio"
    assert get_sms_provider(tenant).name == "twilio"
    context.config["COMMUNICATIONS_PROVIDER_OVERRIDES"] = {tenant.id: {"voice": "sip"}}
    assert get_voice_provider(tenant).name == "sip"
    assert get_messaging_provider(tenant).name == "twilio"
    assert get_voice_provider(SimpleNamespace(id="another-tenant")).name == "twilio"


def test_channel_defaults_can_differ(context, tenant):
    context.config["VOICE_PROVIDER_DEFAULT"] = "sip"
    assert get_voice_provider(tenant).name == "sip"
    assert get_messaging_provider(tenant).name == "twilio"


@pytest.mark.parametrize("channel", ["voice", "whatsapp", "sms"])
def test_unknown_provider_fails_explicitly(context, tenant, channel):
    context.config[f"{channel.upper()}_PROVIDER_DEFAULT"] = "unimplemented"
    getter = {
        "voice": get_voice_provider,
        "whatsapp": get_messaging_provider,
        "sms": get_sms_provider,
    }[channel]
    with pytest.raises(ProviderNotConfigured):
        getter(tenant)


def test_unknown_channel_rejected(context, tenant):
    with pytest.raises(ValueError):
        provider_name(tenant, "other")


def test_sip_never_claims_working_calls(tenant):
    sip = SipVoiceProvider()
    assert not sip.is_configured()
    assert not sip.browser_calling_configured()
    with pytest.raises(ProviderNotConfigured):
        sip.initiate_call(tenant, to="+447123456789", instructions_url="https://example.test")
    with pytest.raises(UnsupportedCapability):
        sip.generate_client_token(tenant, object())


def test_capabilities_are_channel_specific():
    TwilioVoiceProvider().capabilities.require("browser_calling")
    TwilioMessagingProvider().capabilities.require("whatsapp")
    TwilioSMSProvider().capabilities.require("sms")
    with pytest.raises(UnsupportedCapability):
        TwilioVoiceProvider().capabilities.require("whatsapp")
    with pytest.raises(UnsupportedCapability):
        TwilioMessagingProvider().capabilities.require("sms")


def test_sms_provider_needs_a_sendable_number(context, tenant):
    context.config["TWILIO_ACCOUNT_SID"] = "AC123"
    context.config["TWILIO_AUTH_TOKEN"] = "token123"
    provider = TwilioSMSProvider()
    assert provider.configuration_error(tenant) is None  # tenant fixture has voice_phone_number

    bare = SimpleNamespace(
        id="tenant-b",
        communication_settings=SimpleNamespace(
            communications_enabled=True, voice_phone_number=None, messaging_service_sid=None
        ),
    )
    assert "SMS-capable number" in provider.configuration_error(bare)


def test_sms_send_uses_messaging_service_sid_when_set(monkeypatch):
    provider = TwilioSMSProvider()
    tenant = SimpleNamespace(
        id="tenant-c",
        communication_settings=SimpleNamespace(
            communications_enabled=True,
            voice_phone_number="+441234567890",
            messaging_service_sid="MG123",
        ),
    )
    fake_client = Mock()
    fake_client.messages.create.return_value = SimpleNamespace(sid="SM1", status="queued")
    monkeypatch.setattr(
        "app.communications.providers.twilio.get_twilio_client_for_garage", lambda _: fake_client
    )
    result = provider.send_sms(tenant, to="+447123456789", body="Hi")
    assert (result.interaction_id, result.status) == ("SM1", "queued")
    fake_client.messages.create.assert_called_once_with(
        to="+447123456789", body="Hi", messaging_service_sid="MG123"
    )


def test_voice_normalisation_preserves_payload_values():
    provider = TwilioVoiceProvider()
    event = provider.normalise_inbound(
        {"CallSid": "CA123", "To": "+441234", "From": "+447123", "CallStatus": "ringing"}
    )
    assert (event.provider, event.channel, event.interaction_id) == ("twilio", "VOICE", "CA123")
    assert (event.from_address, event.to_address, event.status) == ("+447123", "+441234", "ringing")
    status = provider.normalise_status(
        {"CallSid": "CA123", "CallStatus": "completed", "CallDuration": "42", "ErrorCode": "123"}
    )
    assert (status.interaction_id, status.status, status.duration_seconds, status.error_code) == (
        "CA123",
        "completed",
        42,
        "123",
    )


@pytest.mark.parametrize("duration", [None, "", "bad", "-1", "1.5"])
def test_voice_duration_normalisation_keeps_legacy_rules(duration):
    event = TwilioVoiceProvider().normalise_status({"CallDuration": duration})
    assert event.duration_seconds is None
    assert event.status == "unknown"


def test_messaging_normalisation_preserves_addresses_and_body():
    provider = TwilioMessagingProvider()
    event = provider.normalise_inbound(
        {
            "MessageSid": "SM123",
            "From": "whatsapp:+447123",
            "To": "whatsapp:+441234",
            "Body": " Hi ",
        }
    )
    assert (event.provider, event.channel, event.interaction_id) == ("twilio", "WHATSAPP", "SM123")
    assert (event.from_address, event.to_address, event.body, event.status) == (
        "whatsapp:+447123",
        "whatsapp:+441234",
        " Hi ",
        "received",
    )
    event = provider.normalise_status(
        {
            "MessageSid": "SM123",
            "MessageStatus": "failed",
            "ErrorCode": "99999",
            "ErrorMessage": "reason",
        }
    )
    assert (event.interaction_id, event.status, event.error_code, event.error_message) == (
        "SM123",
        "failed",
        "99999",
        "Not delivered — WhatsApp/SMS provider error 99999.",
    )


class FakeProvider:
    name = "fake"
    capabilities = Capabilities(outbound_voice=True, whatsapp=True, sms=True)

    def configuration_error(self, garage):
        return None

    def initiate_call(self, garage, *, to, instructions_url):
        assert to == "+447123456789"
        assert instructions_url == "https://example.test/instructions"
        return SendResult("fake-call-1", "queued")

    def send_message(self, garage, *, to, body):
        assert to == "whatsapp:+447123456789"
        assert body == "Hello"
        return SendResult("fake-message-1", "sent")

    def send_sms(self, garage, *, to, body):
        assert to == "+447123456789"
        assert body == "Hello"
        return SendResult("fake-sms-1", "sent")


@pytest.mark.parametrize("channel", ["voice", "whatsapp", "sms"])
def test_core_send_uses_fake_provider_without_twilio_configuration(
    context, tenant, monkeypatch, channel
):
    fake = FakeProvider()
    provider_getter = {
        "voice": "get_voice_provider",
        "whatsapp": "get_messaging_provider",
        "sms": "get_sms_provider",
    }[channel]
    monkeypatch.setattr(service, provider_getter, lambda _: fake)
    log = Mock(side_effect=lambda **fields: SimpleNamespace(**fields))
    monkeypatch.setattr(service, "_create_log", log)
    if channel == "voice":
        result = service.initiate_voice_call(
            garage=tenant, to="07123 456789", instructions_url="https://example.test/instructions"
        )
        assert (result.external_id, result.call_sid, result.status) == (
            "fake-call-1",
            "fake-call-1",
            "queued",
        )
    elif channel == "whatsapp":
        result = service.send_whatsapp_message(garage=tenant, to="07123 456789", body="Hello")
        assert (result.external_id, result.status) == ("fake-message-1", "sent")
    else:
        result = service.send_sms_message(garage=tenant, to="07123 456789", body="Hello")
        assert (result.external_id, result.status) == ("fake-sms-1", "sent")
    assert result.external_provider == "fake"
    assert result.garage_id == tenant.id


def test_sms_skipped_when_provider_not_configured(context, tenant, monkeypatch):
    log = Mock(side_effect=lambda **fields: SimpleNamespace(**fields))
    monkeypatch.setattr(service, "_create_log", log)
    result = service.send_sms_message(garage=tenant, to="07123 456789", body="Hello")
    assert result.status == "SKIPPED_NOT_CONFIGURED"
    assert result.external_provider == "twilio"


def test_unsupported_capability_records_failure(context, tenant, monkeypatch):
    fake = FakeProvider()
    fake.capabilities = Capabilities()
    monkeypatch.setattr(service, "get_voice_provider", lambda _: fake)
    monkeypatch.setattr(service, "_create_log", lambda **fields: SimpleNamespace(**fields))
    result = service.initiate_voice_call(
        garage=tenant, to="07123 456789", instructions_url="unused"
    )
    assert result.status == "FAILED"
    assert "outbound_voice" in result.error_message


def test_sip_selected_send_is_skipped_not_successful(context, tenant, monkeypatch):
    context.config["VOICE_PROVIDER_DEFAULT"] = "sip"
    monkeypatch.setattr(service, "_create_log", lambda **fields: SimpleNamespace(**fields))
    result = service.initiate_voice_call(
        garage=tenant, to="07123 456789", instructions_url="unused"
    )
    assert result.status == "SKIPPED_NOT_CONFIGURED"
    assert result.external_provider == "sip"
    assert result.external_id is None
