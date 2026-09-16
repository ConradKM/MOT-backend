"""Small transport contracts. Provider SDK objects never cross this boundary."""

from dataclasses import dataclass
from typing import Protocol


class ProviderNotConfigured(RuntimeError):
    pass


class UnsupportedCapability(RuntimeError):
    pass


class ProviderFailure(RuntimeError):
    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Capabilities:
    inbound_voice: bool = False
    outbound_voice: bool = False
    browser_calling: bool = False
    whatsapp: bool = False
    sms: bool = False

    def require(self, capability: str) -> None:
        if not getattr(self, capability, False):
            raise UnsupportedCapability(f"Provider does not support {capability}.")


@dataclass(frozen=True)
class SendResult:
    interaction_id: str
    status: str


@dataclass(frozen=True)
class InboundEvent:
    provider: str
    channel: str
    interaction_id: str | None
    from_address: str
    to_address: str
    status: str = "received"
    body: str = ""


@dataclass(frozen=True)
class StatusEvent:
    provider: str
    channel: str
    interaction_id: str | None
    status: str
    duration_seconds: int | None = None
    error_code: str | None = None
    error_message: str | None = None


class VoiceProvider(Protocol):
    name: str
    capabilities: Capabilities

    def is_configured(self) -> bool: ...
    def configuration_error(self, garage) -> str | None: ...
    def initiate_call(self, garage, *, to: str, instructions_url: str) -> SendResult: ...
    def browser_calling_configured(self) -> bool: ...
    def generate_client_token(self, garage, employee) -> tuple[str, str]: ...


class MessagingProvider(Protocol):
    name: str
    capabilities: Capabilities

    def is_configured(self) -> bool: ...
    def configuration_error(self, garage) -> str | None: ...
    def send_message(self, garage, *, to: str, body: str) -> SendResult: ...


class SMSProvider(Protocol):
    """Plain-SMS transport - deliberately its own Protocol rather than reused
    from MessagingProvider (which is WhatsApp-shaped: 'whatsapp:'-prefixed
    addresses, a WhatsApp sender). Twilio can back both today from the same
    account, but a future SMS-only/SIP provider must be swappable here
    without touching WhatsApp at all - see app/communications/service.py::
    send_sms_message and docs/SMS_PROVIDERS.md."""

    name: str
    capabilities: Capabilities

    def is_configured(self) -> bool: ...
    def configuration_error(self, garage) -> str | None: ...
    def send_sms(self, garage, *, to: str, body: str) -> SendResult: ...
