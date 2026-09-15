"""Twilio transport and payload edge, wrapping the existing production calls."""

from flask import current_app
from twilio.base.exceptions import TwilioRestException

from ..client import get_twilio_client_for_garage
from ..config import garage_communications_enabled, is_twilio_configured
from ..delivery_status import describe_delivery_failure
from ..security import validate_twilio_request
from ..voice_calling import browser_calling_configured, build_voice_access_token
from .base import Capabilities, InboundEvent, ProviderFailure, SendResult, StatusEvent


def failure(exc: Exception) -> ProviderFailure:
    if isinstance(exc, TwilioRestException):
        code = str(exc.code) if exc.code is not None else None
        message = describe_delivery_failure(code)
        return ProviderFailure(
            message or exc.msg or "The messaging provider rejected the message.", code
        )
    return ProviderFailure(str(exc))


def _configuration_error(garage) -> str | None:
    if not is_twilio_configured():
        return "Twilio is not configured for this deployment."
    if not garage_communications_enabled(garage):
        return "Communications are not enabled for this business."
    return None


class TwilioVoiceProvider:
    name = "twilio"
    capabilities = Capabilities(inbound_voice=True, outbound_voice=True, browser_calling=True)
    is_configured = staticmethod(is_twilio_configured)
    validate_webhook = staticmethod(validate_twilio_request)
    browser_calling_configured = staticmethod(browser_calling_configured)
    generate_client_token = staticmethod(build_voice_access_token)

    def configuration_error(self, garage) -> str | None:
        error = _configuration_error(garage)
        if error:
            return error
        if not garage.communication_settings.voice_phone_number:
            return "No voice number configured for this business."
        return None

    def initiate_call(self, garage, *, to: str, instructions_url: str) -> SendResult:
        client = get_twilio_client_for_garage(garage)
        assert client is not None
        try:
            call = client.calls.create(
                from_=garage.communication_settings.voice_phone_number,
                to=to,
                url=instructions_url,
            )
            return SendResult(call.sid, call.status)
        except Exception as exc:
            raise failure(exc) from exc

    def normalise_inbound(self, payload) -> InboundEvent:
        return InboundEvent(
            self.name,
            "VOICE",
            payload.get("CallSid"),
            payload.get("From", ""),
            payload.get("To", ""),
            payload.get("CallStatus") or "received",
        )

    def normalise_status(self, payload) -> StatusEvent:
        duration = payload.get("CallDuration")
        return StatusEvent(
            self.name,
            "VOICE",
            payload.get("CallSid"),
            payload.get("CallStatus") or "unknown",
            duration_seconds=int(duration) if duration and duration.isdigit() else None,
            error_code=payload.get("ErrorCode") or None,
        )


class TwilioMessagingProvider:
    name = "twilio"
    capabilities = Capabilities(whatsapp=True)
    is_configured = staticmethod(is_twilio_configured)
    validate_webhook = staticmethod(validate_twilio_request)

    def configuration_error(self, garage) -> str | None:
        error = _configuration_error(garage)
        if error:
            return error
        settings = garage.communication_settings
        if not (settings.whatsapp_sender or settings.messaging_service_sid):
            return "No WhatsApp sender configured for this business."
        return None

    def send_message(self, garage, *, to: str, body: str) -> SendResult:
        settings = garage.communication_settings
        client = get_twilio_client_for_garage(garage)
        assert client is not None
        send_kwargs: dict = {"to": to, "body": body}
        if settings.messaging_service_sid:
            send_kwargs["messaging_service_sid"] = settings.messaging_service_sid
        else:
            send_kwargs["from_"] = settings.whatsapp_sender
        base = (current_app.config.get("PUBLIC_API_BASE_URL") or "").rstrip("/")
        if base.startswith("https://"):
            send_kwargs["status_callback"] = f"{base}/api/webhooks/twilio/whatsapp/status"
        try:
            message = client.messages.create(**send_kwargs)
            return SendResult(message.sid, message.status)
        except Exception as exc:
            raise failure(exc) from exc

    def normalise_inbound(self, payload) -> InboundEvent:
        return InboundEvent(
            self.name,
            "WHATSAPP",
            payload.get("MessageSid"),
            payload.get("From", ""),
            payload.get("To", ""),
            body=payload.get("Body", ""),
        )

    def normalise_status(self, payload) -> StatusEvent:
        status = payload.get("MessageStatus") or "unknown"
        code = payload.get("ErrorCode") or None
        return StatusEvent(
            self.name,
            "WHATSAPP",
            payload.get("MessageSid"),
            status,
            error_code=code,
            error_message=describe_delivery_failure(code, status)
            or payload.get("ErrorMessage")
            or None,
        )
