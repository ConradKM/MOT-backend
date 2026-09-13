"""Future SIP transport boundary only; no carrier, media, or working calls."""

from .base import Capabilities, ProviderNotConfigured, SendResult


class SipVoiceProvider:
    name = "sip"
    capabilities = Capabilities()

    def is_configured(self) -> bool:
        return False

    def configuration_error(self, garage) -> str:
        return "SIP voice provider is not implemented or configured."

    def initiate_call(self, garage, *, to: str, instructions_url: str) -> SendResult:
        raise ProviderNotConfigured(self.configuration_error(garage))

    def browser_calling_configured(self) -> bool:
        return False

    def generate_client_token(self, garage, employee) -> tuple[str, str]:
        self.capabilities.require("browser_calling")
        raise ProviderNotConfigured(self.configuration_error(garage))
