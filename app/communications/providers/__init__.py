"""Independent channel selection; existing tenants implicitly use Twilio.

Overrides are deployment-controlled, keyed by immutable garage UUID, never
request input. No provisioning or tenant data writes occur here.
"""

from flask import current_app

from .base import MessagingProvider, ProviderNotConfigured, SMSProvider, VoiceProvider
from .sip import SipVoiceProvider
from .twilio import TwilioMessagingProvider, TwilioSMSProvider, TwilioVoiceProvider


def provider_name(garage, channel: str) -> str:
    if channel not in {"voice", "whatsapp", "sms"}:
        raise ValueError(f"Unknown communications channel: {channel}")
    cfg = current_app.config
    override = cfg.get("COMMUNICATIONS_PROVIDER_OVERRIDES", {}).get(str(garage.id), {})
    return str(override.get(channel, cfg.get(f"{channel.upper()}_PROVIDER_DEFAULT", "twilio")))


def get_voice_provider(garage) -> VoiceProvider:
    name = provider_name(garage, "voice")
    if name == "twilio":
        return TwilioVoiceProvider()
    if name == "sip":
        return SipVoiceProvider()
    raise ProviderNotConfigured(f"Unknown voice provider: {name}")


def get_messaging_provider(garage) -> MessagingProvider:
    name = provider_name(garage, "whatsapp")
    if name == "twilio":
        return TwilioMessagingProvider()
    raise ProviderNotConfigured(f"Unknown WhatsApp provider: {name}")


def get_sms_provider(garage) -> SMSProvider:
    name = provider_name(garage, "sms")
    if name == "twilio":
        return TwilioSMSProvider()
    raise ProviderNotConfigured(f"Unknown SMS provider: {name}")
