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
from collections.abc import Callable
from datetime import date as date_cls
from datetime import time as time_cls

from app.conversation import actions

# Sent to Twilio as a valid-but-terse function_call_output when a tool raises
# unexpectedly - the model still gets a turn to react (e.g. apologise and
# hand off) rather than the whole bridge crashing mid-call.
_TOOL_ERROR = {"ok": False, "error": "That couldn't be completed right now."}

TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "name": "get_business_info",
        "description": (
            "Get this business's name, contact details, and opening hours. Use this if the "
            "caller asks for the address, phone number, or when the business is open."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "get_appointment_types",
        "description": (
            "List the services (appointment types) this business currently offers, with a "
            "short description, typical duration, and price where set."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "get_available_slots",
        "description": (
            "Get real, currently-available appointment times for a given date and service. "
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
            "Submit a booking request for this business to review - not an instant "
            "confirmation. Only call this once you have a real available slot (checked with "
            "get_available_slots in this same call), the caller's name, a contact mobile "
            "number, and their vehicle registration."
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
                "notes": {"type": "string", "description": "Anything else worth telling staff."},
            },
            "required": [
                "appointment_type_id",
                "date",
                "time",
                "first_name",
                "last_name",
                "vehicle_registration",
            ],
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
]

# Tool names that end the call once handled - the bridge checks this after
# dispatch to close the session gracefully instead of waiting on more audio.
CALL_ENDING_TOOLS = frozenset({"request_human_handoff"})


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
    open_slots = [s["start"] for s in payload.get("slots", []) if s.get("status") != "booked"]
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


def _tool_create_booking(
    garage,
    caller_phone_e164: str,
    *,
    appointment_type_id: str,
    date: str,
    time: str,
    first_name: str,
    last_name: str,
    vehicle_registration: str,
    phone: str | None = None,
    notes: str | None = None,
    **_args,
) -> dict:
    appointment_type = _find_appointment_type(garage, appointment_type_id)
    if appointment_type is None:
        return {"ok": False, "error": "Unknown appointment_type_id."}

    day = _parse_date(date)
    slot_time = _parse_time(time)
    if day is None or slot_time is None:
        return {"ok": False, "error": "date must be YYYY-MM-DD and time must be HH:MM."}

    contact_phone = (phone or caller_phone_e164 or "").strip()
    customer = actions.find_customer(garage, contact_phone) if contact_phone else None

    booking_request, reason = actions.create_booking_request(
        garage,
        customer=customer,
        first_name=first_name,
        last_name=last_name,
        phone_e164=contact_phone,
        email=None,
        vehicle_registration=vehicle_registration,
        appointment_type=appointment_type,
        preferred_date=day,
        preferred_time=slot_time,
        notes=notes,
    )
    if booking_request is None:
        return {
            "ok": False,
            "error": f"That slot is no longer available ({reason}). Offer to check another time.",
        }
    return {
        "ok": True,
        "booking_reference": booking_request.booking_reference,
        "status": booking_request.status,
    }


def _tool_request_human_handoff(garage, caller_phone_e164: str, *, reason: str, **_args) -> dict:
    customer = actions.find_customer(garage, caller_phone_e164) if caller_phone_e164 else None
    callback = actions.create_callback_request(
        garage, customer=customer, phone_e164=caller_phone_e164, reason=reason
    )
    return {"ok": True, "callback_id": str(callback.id)}


_HANDLERS: dict[str, Callable[..., dict]] = {
    "get_business_info": _tool_get_business_info,
    "get_appointment_types": _tool_get_appointment_types,
    "get_available_slots": _tool_get_available_slots,
    "create_booking": _tool_create_booking,
    "request_human_handoff": _tool_request_human_handoff,
}


def dispatch_tool(garage, caller_phone_e164: str, name: str, arguments_json: str) -> str:
    """Execute one tool call by name, tenant-scoped to ``garage``. Always
    returns a JSON string (never raises) - the caller (app/ws/openai_voice.py)
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

    try:
        if name in ("create_booking", "request_human_handoff"):
            result = handler(garage, caller_phone_e164, **arguments)
        else:
            result = handler(garage, **arguments)
    except TypeError:
        return json.dumps({"ok": False, "error": "Missing or invalid arguments."})
    except Exception:  # noqa: BLE001 - a tool failure must never crash the call
        return json.dumps(_TOOL_ERROR)

    return json.dumps(result)
