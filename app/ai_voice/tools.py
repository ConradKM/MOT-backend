"""The tools the OpenAI Realtime model can call during a phone call, and the
tenant-scoped dispatcher that actually executes one.

Every tool here does exactly what the conversation engine's own action layer
already does for WhatsApp (app/conversation/actions.py) - this is a second,
independent caller of those same tenant-scoped functions, never a parallel
booking/availability implementation. ``dispatch_tool`` is always called with
an already-resolved ``garage`` (from the Twilio call's own dialled number,
never anything the model itself could claim) and the caller's own phone
number from the live call - the model can select *what* to look up or book,
never *whose* business or *whose* phone number.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date as date_cls
from datetime import time as time_cls

from flask import current_app

from app.booking_flow import vehicle_details
from app.communications.service import send_sms_message
from app.conversation import actions
from app.extensions import db
from app.models.ai_voice_faq import GarageVoiceFAQ
from app.models.booking_request import BookingRequest
from app.models.communications.communication_log import STATUS_SKIPPED_NOT_CONFIGURED
from app.payments.money import DepositConfigError, calculate_deposit_minor, minor_to_decimal
from app.payments.providers.base import PaymentProviderError
from app.payments.service import PaymentUnavailableError
from app.phone import InvalidPhoneNumberError, normalize_uk_phone
from app.public_booking import availability

logger = logging.getLogger(__name__)

# Sent to Twilio as a valid-but-terse function_call_output when a tool raises
# unexpectedly - the model still gets a turn to react (e.g. apologise and
# hand off) rather than the whole bridge crashing mid-call.
_TOOL_ERROR = {"ok": False, "error": "That couldn't be completed right now."}


@dataclass
class VoiceToolState:
    """Per-call proof of the live slots the agent was allowed to offer.

    Prompts can ask the model to follow a booking sequence; this small state
    machine enforces the non-negotiable part.  It is intentionally short
    lived: availability must be checked in *this* phone call, and CoMaz
    revalidates it again when the request is committed.
    """

    offered_slots: set[tuple[str, str, str]] = field(default_factory=set)
    looked_up_appointment_ids: set[str] = field(default_factory=set)

    def record_availability(self, arguments: dict, result: dict) -> None:
        if not result.get("ok"):
            return
        appointment_type_id = str(arguments.get("appointment_type_id", ""))
        day = str(arguments.get("date", ""))
        for slot in result.get("available_times", []):
            self.offered_slots.add((appointment_type_id, day, str(slot)))

    def includes_slot(self, arguments: dict) -> bool:
        parsed_time = _parse_time(arguments.get("time", ""))
        if parsed_time is None:
            return False
        return (
            str(arguments.get("appointment_type_id", "")),
            str(arguments.get("date", "")),
            parsed_time.strftime("%H:%M"),
        ) in self.offered_slots

    def includes_time(self, arguments: dict) -> bool:
        """Whether a live availability lookup returned this date/time.

        Rescheduling derives the service from the existing appointment, so
        this is deliberately a date/time proof only; the action layer still
        revalidates the slot against that appointment's real service.
        """
        parsed_time = _parse_time(arguments.get("time", ""))
        if parsed_time is None:
            return False
        day = str(arguments.get("date", ""))
        formatted_time = parsed_time.strftime("%H:%M")
        return any(
            offered_day == day and offered_time == formatted_time
            for _appointment_type_id, offered_day, offered_time in self.offered_slots
        )

    def record_appointments(self, result: dict) -> None:
        if not result.get("ok"):
            return
        self.looked_up_appointment_ids.update(
            str(appointment["id"])
            for appointment in result.get("appointments", [])
            if isinstance(appointment, dict) and appointment.get("id")
        )


TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "name": "get_business_info",
        "description": (
            "Authoritative CoMaz source for this business's contact details and opening hours. "
            "You MUST call it before answering any question about address, phone number, or hours."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "get_appointment_types",
        "description": (
            "Authoritative CoMaz source for services, prices, durations, and which vehicle "
            "details (registration, make, model) each service asks for - 'required', "
            "'optional' or 'not_asked'. You MUST call it before naming, describing, pricing, "
            "or booking a service."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "get_available_slots",
        "description": (
            "Authoritative live CoMaz availability for a given date and selected service. "
            "If nothing is free on that date, this also returns the next few dates that do "
            "have availability. Always call this before offering a time to the caller - never "
            "state a time without checking it first."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "appointment_type_id": {
                    "type": "string",
                    "description": "The id of the service, from get_appointment_types.",
                },
                "date": {
                    "type": "string",
                    "description": "The date to check, as YYYY-MM-DD.",
                },
            },
            "required": ["appointment_type_id", "date"],
        },
    },
    {
        "type": "function",
        "name": "create_booking",
        "description": (
            "Submit a CoMaz booking request for staff review - never an instant confirmation. "
            "Only call after get_appointment_types and get_available_slots have identified the "
            "selected service and real slot, the caller has explicitly confirmed all details, "
            "and you have their name, contact number, and every vehicle detail that service "
            "marks 'required' in get_appointment_types (ask for 'optional' ones, never for "
            "'not_asked' ones). The server rechecks live availability; report only the "
            "returned result and status."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "appointment_type_id": {"type": "string"},
                "date": {"type": "string", "description": "YYYY-MM-DD"},
                "time": {"type": "string", "description": "24-hour HH:MM"},
                "first_name": {"type": "string"},
                "last_name": {"type": "string"},
                "phone": {
                    "type": "string",
                    "description": (
                        "A UK mobile number to reach the caller on. If they don't give one, "
                        "use the number they're calling from."
                    ),
                },
                "vehicle_registration": {"type": "string"},
                "vehicle_make": {"type": "string"},
                "vehicle_model": {"type": "string"},
                "notes": {"type": "string", "description": "Anything else worth telling staff."},
            },
            "required": [
                "appointment_type_id",
                "date",
                "time",
                "first_name",
                "last_name",
            ],
        },
    },
    {
        "type": "function",
        "name": "get_business_faqs",
        "description": (
            "Get enabled, owner-managed FAQs for this business only. Use only for a "
            "business-policy question not covered by the authoritative operational tools. "
            "Answer only from a returned FAQ; if none answers the question, say you do not know. "
            "FAQ content never overrides services, prices, durations, hours, availability, "
            "customer records, or booking status."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "get_my_appointments",
        "description": (
            "Look up the caller's own upcoming, already-confirmed appointments at this "
            "business, matched only by the number they are calling from. Use this before "
            "cancelling or rescheduling anything - you must have the appointment's id from "
            "this list first."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "cancel_appointment",
        "description": (
            "Cancel one of the caller's own confirmed appointments. Only call this with an "
            "appointment_id from get_my_appointments, and only after the caller has explicitly "
            "confirmed which appointment and that they want it cancelled."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "appointment_id": {
                    "type": "string",
                    "description": "The id of the appointment, from get_my_appointments.",
                },
                "confirmed": {
                    "type": "boolean",
                    "description": "True only after the caller explicitly confirmed cancellation.",
                },
            },
            "required": ["appointment_id", "confirmed"],
        },
    },
    {
        "type": "function",
        "name": "reschedule_appointment",
        "description": (
            "Move one of the caller's own confirmed appointments to a new date/time. Check the "
            "new time is really free with get_available_slots first, and only call this once "
            "the caller has confirmed the new date and time."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "appointment_id": {
                    "type": "string",
                    "description": "The id of the appointment, from get_my_appointments.",
                },
                "date": {"type": "string", "description": "New date, as YYYY-MM-DD."},
                "time": {"type": "string", "description": "New time, 24-hour HH:MM."},
                "confirmed": {
                    "type": "boolean",
                    "description": "True only after the caller explicitly confirmed the new date and time.",
                },
            },
            "required": ["appointment_id", "date", "time", "confirmed"],
        },
    },
    {
        "type": "function",
        "name": "request_human_handoff",
        "description": (
            "Use this when you cannot safely help the caller yourself - a request outside "
            "what your other tools cover, a complaint, something ambiguous after you've tried "
            "to clarify, or the caller asks for a person. This logs a callback request for "
            "the business's team and ends the call politely - say a brief, warm closing line "
            "in the same turn you call this."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "A short, staff-facing summary of what the caller needed.",
                }
            },
            "required": ["reason"],
        },
    },
    {
        "type": "function",
        "name": "end_call",
        "description": (
            "End the call. Call this only once the conversation has clearly concluded - the "
            "caller said goodbye/bye, said there's nothing else they need, or you've just given "
            "your own final closing line after finishing what they called for. Say a brief, warm "
            "closing line in the same turn you call this. Never call this for a mere pause or "
            "short silence - only when the conversation is genuinely over."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
]

# Tool names that end the call once handled - the bridge checks this after
# dispatch to close the session gracefully instead of waiting on more audio.
CALL_ENDING_TOOLS = frozenset({"request_human_handoff", "end_call"})


def _parse_date(value: str) -> date_cls | None:
    try:
        return date_cls.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def _parse_time(value: str) -> time_cls | None:
    try:
        hours, minutes = value.split(":", 1)
        return time_cls(int(hours), int(minutes))
    except (ValueError, TypeError, AttributeError):
        return None


def _clean(value, max_length: int, *, upper: bool = False) -> str | None:
    text = " ".join(str(value or "").split())[:max_length]
    if upper:
        text = text.upper()
    return text or None


def _find_appointment_type(garage, appointment_type_id: str):
    for t in actions.get_appointment_types(garage):
        if str(t.id) == str(appointment_type_id):
            return t
    return None


def _tool_get_business_info(garage, **_args) -> dict:
    hours = actions.get_business_hours(garage)
    return {
        "ok": True,
        "name": garage.name,
        "phone": garage.phone,
        "email": garage.email,
        "address": garage.address,
        "opening_hours": {
            str(weekday): {
                "opens_at": opens.strftime("%H:%M") if opens else None,
                "closes_at": closes.strftime("%H:%M") if closes else None,
                "is_closed": is_closed,
            }
            for weekday, (opens, closes, is_closed) in hours.items()
        },
    }


def _deposit_amount(appointment_type) -> str | None:
    """The configured deposit in major units (e.g. "30.00"), or ``None`` if
    this type doesn't require one - never guessed/calculated when the
    underlying config can't support it (mirrors
    app/payments/service.py::create_deposit_hold's own assumptions)."""
    if not appointment_type.deposit_required:
        return None
    if appointment_type.deposit_type is None or appointment_type.deposit_value is None:
        return None
    try:
        minor = calculate_deposit_minor(
            deposit_type=appointment_type.deposit_type,
            deposit_value=appointment_type.deposit_value,
            base_price=appointment_type.base_price,
        )
    except DepositConfigError:
        return None
    return str(minor_to_decimal(minor))


def _tool_get_appointment_types(garage, **_args) -> dict:
    types = actions.get_appointment_types(garage)
    return {
        "ok": True,
        "appointment_types": [
            {
                "id": str(t.id),
                "name": t.name,
                "description": t.description,
                "base_price": str(t.base_price) if t.base_price is not None else None,
                "default_duration_minutes": t.default_duration_minutes,
                "vehicle_details": vehicle_details.requirements_for(garage.id, t.id),
                "deposit_required": t.deposit_required,
                "deposit_amount": _deposit_amount(t),
                "deposit_currency": t.deposit_currency if t.deposit_required else None,
            }
            for t in types
        ],
    }


def _tool_get_available_slots(garage, *, appointment_type_id: str, date: str, **_args) -> dict:
    appointment_type = _find_appointment_type(garage, appointment_type_id)
    if appointment_type is None:
        return {"ok": False, "error": "Unknown appointment_type_id."}

    day = _parse_date(date)
    if day is None:
        return {"ok": False, "error": "date must be YYYY-MM-DD."}

    payload = actions.get_availability_for_day(garage, day, appointment_type=appointment_type)
    # Be intentionally positive here: the public availability service can add
    # explanatory non-bookable statuses later without accidentally making one
    # a voice-bookable slot.  ``limited`` still is genuinely bookable, just
    # with low remaining capacity.
    open_slots = [
        s["start"]
        for s in payload.get("slots", [])
        if s.get("status") in (availability.SLOT_AVAILABLE, availability.SLOT_LIMITED)
    ]
    result: dict = {
        "ok": True,
        "date": date,
        "is_open": payload.get("is_open", False),
        "available_times": open_slots,
    }
    if not open_slots:
        next_days = actions.find_next_available_days(
            garage, appointment_type=appointment_type, start_day=day
        )
        result["next_available_dates"] = [d.isoformat() for d in next_days]
    return result


def _tool_get_business_faqs(garage, **_args) -> dict:
    """Only active knowledge is exposed, scoped by the already-resolved tenant."""
    faqs = (
        GarageVoiceFAQ.query.filter_by(garage_id=garage.id, is_enabled=True, archived_at=None)
        .order_by(GarageVoiceFAQ.order, GarageVoiceFAQ.question)
        .all()
    )
    return {
        "ok": True,
        "faqs": [{"question": faq.question, "answer": faq.answer} for faq in faqs],
    }


def _tool_create_booking(
    garage,
    caller_phone_e164: str,
    *,
    appointment_type_id: str,
    date: str,
    time: str,
    first_name: str,
    last_name: str,
    vehicle_registration: str | None = None,
    vehicle_make: str | None = None,
    vehicle_model: str | None = None,
    phone: str | None = None,
    notes: str | None = None,
    voice_tool_call_id: str | None = None,
    voice_call_id: str | None = None,
    **_args,
) -> dict:
    appointment_type = _find_appointment_type(garage, appointment_type_id)
    if appointment_type is None:
        return {"ok": False, "error": "Unknown appointment_type_id."}

    day = _parse_date(date)
    slot_time = _parse_time(time)
    if day is None or slot_time is None:
        return {"ok": False, "error": "date must be YYYY-MM-DD and time must be HH:MM."}

    # Required exactly when this business's own booking form requires it for
    # this service - never more (a business that doesn't book vehicles is
    # never made to collect a registration). Details the caller volunteered
    # are kept; make/model only against a registration, as on the public form
    # (app/booking_flow/answers.py::tracked_item_kwargs).
    requirements = vehicle_details.requirements_for(garage.id, appointment_type.id)
    vehicle = {
        "registration": _clean(vehicle_registration, 20, upper=True),
        "make": _clean(vehicle_make, 100),
        "model": _clean(vehicle_model, 100),
    }
    missing = [
        key
        for key, need in requirements.items()
        if need == vehicle_details.REQUIRED and not vehicle[key]
    ]
    if missing:
        return {
            "ok": False,
            "error": f"Ask the caller for their vehicle {' and '.join(missing)} first.",
        }
    if not vehicle["registration"]:
        vehicle["make"] = vehicle["model"] = None

    try:
        contact_phone = normalize_uk_phone((phone or caller_phone_e164 or "").strip())
    except InvalidPhoneNumberError:
        return {"ok": False, "error": "A valid UK mobile contact number is required."}
    # A supplied contact number is useful for a new request but is not proof
    # that this caller owns an existing customer record. Only the number from
    # the SIP call is trusted for customer linkage.
    customer = actions.find_customer(garage, caller_phone_e164) if caller_phone_e164 else None

    booking_kwargs = {
        "customer": customer,
        "first_name": first_name,
        "last_name": last_name,
        "phone_e164": contact_phone,
        "email": None,
        "vehicle_registration": vehicle["registration"],
        "vehicle_make": vehicle["make"],
        "vehicle_model": vehicle["model"],
        "appointment_type": appointment_type,
        "preferred_date": day,
        "preferred_time": slot_time,
        "notes": notes,
        "voice_tool_call_id": voice_tool_call_id,
        "voice_call_id": voice_call_id,
    }

    if appointment_type.deposit_required:
        return _create_deposit_booking(garage, contact_phone=contact_phone, **booking_kwargs)

    booking_request, reason = actions.create_booking_request(garage, **booking_kwargs)
    if booking_request is None:
        return {
            "ok": False,
            "error": f"That slot is no longer available ({reason}). Offer to check another time.",
        }
    return {
        "ok": True,
        "booking_reference": booking_request.booking_reference,
        "status": booking_request.status,
        "service": appointment_type.name,
        "date": booking_request.preferred_date.isoformat(),
        "time": (
            booking_request.preferred_time.strftime("%H:%M")
            if booking_request.preferred_time is not None
            else None
        ),
    }


def _create_deposit_booking(garage, *, contact_phone: str, **booking_kwargs) -> dict:
    """The deposit-required branch of ``_tool_create_booking``: creates an
    AWAITING_PAYMENT hold + provider payment session
    (app/conversation/actions.py::create_deposit_booking_request, which
    reuses the exact same lifecycle as the public booking form's deposit
    step), then texts the caller a link to finish paying on their own phone
    - never asks them to read a card number aloud, and never marks the
    booking as more complete than it actually is."""
    appointment_type = booking_kwargs["appointment_type"]
    try:
        booking_request, reason = actions.create_deposit_booking_request(garage, **booking_kwargs)
    except PaymentUnavailableError:
        return {
            "ok": False,
            "error": (
                "This business can't take deposit payments online right now. "
                "Offer a human callback instead."
            ),
        }
    except (DepositConfigError, PaymentProviderError):
        logger.exception("AI_VOICE_DEPOSIT_HOLD_FAILED garage=%s", garage.id)
        return {
            "ok": False,
            "error": (
                "Couldn't start the deposit payment right now. Offer to try again or a human callback."
            ),
        }

    if booking_request is None:
        return {
            "ok": False,
            "error": f"That slot is no longer available ({reason}). Offer to check another time.",
        }

    deposit_amount = _deposit_amount(appointment_type)
    recovery_token = getattr(booking_request, "deposit_recovery_token", None)
    sms_sent = False
    if recovery_token:
        base_url = str(current_app.config.get("APP_BASE_URL") or "").rstrip("/")
        # The public booking wizard's own resume mechanism
        # (MOT-frontend src/pages/customer/BookingWizard.tsx) already knows
        # how to pick a token up from ?resume=... and drive it through the
        # exact same DepositStep/PaymentCheckout UI a browser abandoning
        # payment mid-flow would land on - no separate payment page.
        pay_url = f"{base_url}/book/{garage.id}?resume={recovery_token}"
        body = (
            f"{garage.name}: Your booking request for {appointment_type.name} on "
            f"{booking_request.preferred_date.isoformat()} requires a "
            f"£{deposit_amount} deposit. Pay securely: {pay_url}"
        )
        log = send_sms_message(
            garage=garage,
            to=contact_phone,
            body=body,
            booking_request=booking_request,
        )
        sms_sent = log.status not in ("FAILED", STATUS_SKIPPED_NOT_CONFIGURED)

    return {
        "ok": True,
        "booking_reference": booking_request.booking_reference,
        "status": booking_request.status,
        "service": appointment_type.name,
        "date": booking_request.preferred_date.isoformat(),
        "time": (
            booking_request.preferred_time.strftime("%H:%M")
            if booking_request.preferred_time is not None
            else None
        ),
        "deposit_required": True,
        "deposit_amount": deposit_amount,
        "deposit_currency": appointment_type.deposit_currency,
        "payment_link_sent_by_sms": sms_sent,
    }


def _tool_get_my_appointments(garage, caller_phone_e164: str, **_args) -> dict:
    customer = actions.find_customer(garage, caller_phone_e164) if caller_phone_e164 else None
    if customer is None:
        return {"ok": True, "appointments": []}
    upcoming = actions.get_upcoming_appointments(garage, customer)
    return {
        "ok": True,
        "appointments": [
            {
                "id": str(a.id),
                "service": a.appointment_type.name if a.appointment_type else None,
                "date": a.start_time.date().isoformat(),
                "time": a.start_time.strftime("%H:%M"),
            }
            for a in upcoming
        ],
    }


def _find_own_appointment(garage, caller_phone_e164: str, appointment_id: str):
    """The caller's own appointment matching ``appointment_id`` - never any
    other customer's, even within the same garage (mirrors
    ``find_customer_vehicle_by_registration``'s ownership check above)."""
    if not caller_phone_e164:
        return None
    customer = actions.find_customer(garage, caller_phone_e164)
    if customer is None:
        return None
    for appointment in actions.get_upcoming_appointments(garage, customer):
        if str(appointment.id) == str(appointment_id):
            return appointment
    return None


def _tool_cancel_appointment(
    garage, caller_phone_e164: str, *, appointment_id: str, confirmed: bool, **_args
) -> dict:
    appointment = _find_own_appointment(garage, caller_phone_e164, appointment_id)
    if appointment is None:
        return {"ok": False, "error": "That appointment couldn't be found on this number."}
    ok, reason = actions.cancel_appointment(garage, appointment)
    if not ok:
        return {"ok": False, "error": f"Couldn't cancel that appointment ({reason})."}
    return {"ok": True}


def _tool_reschedule_appointment(
    garage,
    caller_phone_e164: str,
    *,
    appointment_id: str,
    date: str,
    time: str,
    confirmed: bool,
    **_args,
) -> dict:
    appointment = _find_own_appointment(garage, caller_phone_e164, appointment_id)
    if appointment is None:
        return {"ok": False, "error": "That appointment couldn't be found on this number."}

    day = _parse_date(date)
    slot_time = _parse_time(time)
    if day is None or slot_time is None:
        return {"ok": False, "error": "date must be YYYY-MM-DD and time must be HH:MM."}

    ok, reason = actions.reschedule_appointment(garage, appointment, day, slot_time)
    if not ok:
        return {
            "ok": False,
            "error": f"That new time isn't available ({reason}). Offer to check another time.",
        }
    return {"ok": True, "date": date, "time": time}


def _fallback_transfer_uri(garage) -> str | None:
    """A business's own nominated human-escalation number, if it's set one -
    the same fields the ConversationRelay path already uses for this
    (app/communications/voice_webhooks.py::_escalation_number). Returned as
    a ``tel:`` URI, the shape OpenAI's SIP REFER call expects (see
    app/ai_voice/openai_sip.py::refer_call)."""
    settings = getattr(garage, "communication_settings", None)
    if settings is None:
        return None
    number = settings.voice_escalation_number or settings.voice_fallback_number
    return f"tel:{number}" if number else None


def _tool_request_human_handoff(garage, caller_phone_e164: str, *, reason: str, **_args) -> dict:
    customer = actions.find_customer(garage, caller_phone_e164) if caller_phone_e164 else None
    callback = actions.create_callback_request(
        garage, customer=customer, phone_e164=caller_phone_e164, reason=reason
    )
    # Never exposed to the model as a tool result field it could act on -
    # this is read by app/ai_voice/call_controller.py only, to decide
    # between a blind SIP transfer and a plain hangup once the model's
    # closing line has played. The model is never told a transfer number.
    return {
        "ok": True,
        "callback_id": str(callback.id),
        "_transfer_uri": _fallback_transfer_uri(garage),
    }


def _tool_end_call(garage, **_args) -> dict:
    """No side effect beyond being in CALL_ENDING_TOOLS - it exists purely
    so the model has an explicit, idempotent way to signal 'hang up now'
    once it has said its closing line (app/ai_voice/call_controller.py
    checks CALL_ENDING_TOOLS after dispatch and hangs up after the response
    finishes playing). Calling it more than once in a call is harmless: the
    controller only acts on it once, and there is nothing here to repeat."""
    return {"ok": True}


_HANDLERS: dict[str, Callable[..., dict]] = {
    "get_business_info": _tool_get_business_info,
    "get_appointment_types": _tool_get_appointment_types,
    "get_available_slots": _tool_get_available_slots,
    "get_business_faqs": _tool_get_business_faqs,
    "create_booking": _tool_create_booking,
    "get_my_appointments": _tool_get_my_appointments,
    "cancel_appointment": _tool_cancel_appointment,
    "reschedule_appointment": _tool_reschedule_appointment,
    "request_human_handoff": _tool_request_human_handoff,
    "end_call": _tool_end_call,
}

# Tools that need the live caller's own phone number (to identify them as a
# customer or default a contact field) rather than just the tenant - kept as
# an explicit set (like CALL_ENDING_TOOLS above) so a new caller-scoped tool
# can't silently fall through to the tenant-only call shape below.
_CALLER_SCOPED_TOOLS = frozenset(
    {
        "create_booking",
        "get_my_appointments",
        "cancel_appointment",
        "reschedule_appointment",
        "request_human_handoff",
    }
)


def dispatch_tool(
    garage,
    caller_phone_e164: str,
    name: str,
    arguments_json: str,
    *,
    state: VoiceToolState | None = None,
    tool_call_id: str | None = None,
    call_id: str | None = None,
) -> str:
    """Execute one tool call by name, tenant-scoped to ``garage``. Always
    returns a JSON string (never raises) - the caller (app/ai_voice/call_controller.py)
    sends this straight back to OpenAI as the function_call_output, so a
    tool failure becomes something the model can react to in speech rather
    than a dropped call."""
    handler = _HANDLERS.get(name)
    if handler is None:
        return json.dumps({"ok": False, "error": f"Unknown tool: {name}"})

    try:
        arguments = json.loads(arguments_json) if arguments_json else {}
    except (ValueError, TypeError):
        return json.dumps({"ok": False, "error": "Malformed tool arguments."})

    if not isinstance(arguments, dict):
        return json.dumps({"ok": False, "error": "Tool arguments must be an object."})

    if name in {"cancel_appointment", "reschedule_appointment"}:
        if arguments.get("confirmed") is not True:
            return json.dumps(
                {"ok": False, "error": "The caller must explicitly confirm this change first."}
            )
        if (
            state is not None
            and str(arguments.get("appointment_id", "")) not in state.looked_up_appointment_ids
        ):
            return json.dumps(
                {
                    "ok": False,
                    "error": "Use get_my_appointments in this call before changing an appointment.",
                }
            )
        if (
            name == "reschedule_appointment"
            and state is not None
            and not state.includes_time(arguments)
        ):
            return json.dumps(
                {
                    "ok": False,
                    "error": "That new time has not been returned by live availability in this call.",
                }
            )

    # Only the live controller passes state. Keeping it optional preserves
    # other explicitly-tested internal callers while making the production
    # voice path unable to create a request for a slot it has not actually
    # obtained from CoMaz during this call.
    is_idempotent_retry = bool(
        name == "create_booking"
        and (
            (
                tool_call_id
                and BookingRequest.query.filter_by(
                    garage_id=garage.id, voice_tool_call_id=tool_call_id
                ).first()
            )
            or (
                call_id
                and BookingRequest.query.filter_by(
                    garage_id=garage.id, voice_call_id=call_id
                ).first()
            )
        )
    )
    if (
        name == "create_booking"
        and state is not None
        and not state.includes_slot(arguments)
        and not is_idempotent_retry
    ):
        return json.dumps(
            {
                "ok": False,
                "error": (
                    "That slot has not been returned by live availability in this call. "
                    "Use get_available_slots and offer a returned time first."
                ),
            }
        )

    # This value is from the OpenAI event envelope, never the model's JSON.
    # It is persisted solely to make the mutation safe across reconnects.
    if name == "create_booking" and tool_call_id:
        arguments["voice_tool_call_id"] = tool_call_id
    if name == "create_booking" and call_id:
        arguments["voice_call_id"] = call_id

    try:
        if name in _CALLER_SCOPED_TOOLS:
            result = handler(garage, caller_phone_e164, **arguments)
        else:
            result = handler(garage, **arguments)
    except TypeError:
        return json.dumps({"ok": False, "error": "Missing or invalid arguments."})
    except Exception:  # a tool failure must never crash the call
        logger.exception("AI_VOICE_TOOL_FAILED tool=%s", name)
        # The control greenlet reuses one session for the whole call; left in
        # a failed transaction, every later tool on this call would fail too.
        db.session.rollback()
        return json.dumps(_TOOL_ERROR)

    if name == "get_available_slots" and state is not None:
        state.record_availability(arguments, result)
    if name == "get_my_appointments" and state is not None:
        state.record_appointments(result)
    return json.dumps(result)
