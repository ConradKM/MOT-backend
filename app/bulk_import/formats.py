"""The import formats as the UIs present them: an example, a JSON Schema and
a copyable prompt for whichever AI assistant the user prefers.

The AI step is manual and happens outside this system on purpose: we never
upload a screenshot or call a model. Keeping this text here, rather than in
each frontend, means the owner app and Platform Admin show the same format,
and it can't drift from the rules the importer actually applies.

Prompt placeholders (``{{service_names}}``, ``{{timezone}}``, ``{{statuses}}``)
are filled in by the frontend from what it already has loaded, so the AI
answers with names that will match.
"""

from __future__ import annotations

from typing import Any

from app.appointments.statuses.defaults import DEFAULT_STATUS_KEYS
from app.garages.timezones import DEFAULT_BUSINESS_TIMEZONE
from app.models.appointments.add_on import ADD_ON_MAX_QUANTITY_LIMIT, ADD_ON_STATUSES
from app.models.appointments.appointment_type import APPOINTMENT_TYPE_STATUSES

from .result import MAX_APPOINTMENTS, MAX_BODY_BYTES, MAX_SERVICES
from .services import MAX_ADD_ONS_PER_SERVICE

_NULLABLE_STRING = {"type": ["string", "null"]}

SERVICES_EXAMPLE: dict[str, Any] = {
    "services": [
        {
            "name": "MOT Test",
            "description": "Class 4 MOT",
            "base_price": "54.85",
            "default_duration_minutes": 45,
            "status": "ACTIVE",
            "group": None,
        },
        {
            "name": "Full Service",
            "description": None,
            "base_price": "189.00",
            "default_duration_minutes": 120,
            "status": "ACTIVE",
            "group": "Servicing",
            "add_ons": [
                {
                    "name": "Air-con re-gas",
                    "description": None,
                    "price_delta": "45.00",
                    "duration_delta_minutes": 30,
                    "max_quantity": 1,
                    "exclusivity_group": None,
                    "status": "ACTIVE",
                }
            ],
        },
    ]
}

SERVICES_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Services import",
    "type": "object",
    "required": ["services"],
    "properties": {
        "services": {
            "type": "array",
            "maxItems": MAX_SERVICES,
            "items": {
                "type": "object",
                "required": ["name"],
                "properties": {
                    "name": {"type": "string", "minLength": 1, "maxLength": 100},
                    "description": {"type": ["string", "null"], "maxLength": 500},
                    "base_price": {
                        "type": ["string", "null"],
                        "pattern": r"^\d+(\.\d{1,2})?$",
                        "description": 'Pounds as a decimal string, e.g. "54.85".',
                    },
                    "default_duration_minutes": {
                        "type": ["integer", "null"],
                        "minimum": 1,
                        "maximum": 1440,
                    },
                    "status": {"enum": [*APPOINTMENT_TYPE_STATUSES, None]},
                    "group": {"type": ["string", "null"], "maxLength": 100},
                    "add_ons": {
                        "type": ["array", "null"],
                        "maxItems": MAX_ADD_ONS_PER_SERVICE,
                        "description": "Optional extras for this service only, in display "
                        "order. Ignored if the service already exists.",
                        "items": {
                            "type": "object",
                            "required": ["name"],
                            "properties": {
                                "name": {"type": "string", "minLength": 1, "maxLength": 100},
                                "description": {"type": ["string", "null"], "maxLength": 500},
                                "price_delta": {
                                    "type": ["string", "null"],
                                    "pattern": r"^-?\d+(\.\d{1,2})?$",
                                    "description": "Added to the service price, in pounds; "
                                    'negative for a discount. Default "0.00".',
                                },
                                "duration_delta_minutes": {
                                    "type": ["integer", "null"],
                                    "minimum": -1440,
                                    "maximum": 1440,
                                    "description": "Added to the service duration; default 0.",
                                },
                                "max_quantity": {
                                    "type": ["integer", "null"],
                                    "minimum": 1,
                                    "maximum": ADD_ON_MAX_QUANTITY_LIMIT,
                                    "description": "How many times it can be picked; default 1.",
                                },
                                "exclusivity_group": {
                                    "type": ["string", "null"],
                                    "maxLength": 50,
                                    "description": "At most one add-on with the same label "
                                    "can be picked on one booking.",
                                },
                                "status": {"enum": [*ADD_ON_STATUSES, None]},
                            },
                        },
                    },
                },
            },
        }
    },
}

SERVICES_PROMPT = """\
Extract every service from the attached image (a price list, menu or website \
screenshot) and reply with ONLY a JSON object in exactly this format - no \
explanation, no markdown code fences:

{"services": [
  {"name": "...", "description": null, "base_price": "54.85",
   "default_duration_minutes": 45, "status": "ACTIVE", "group": null,
   "add_ons": [
     {"name": "...", "description": null, "price_delta": "10.00",
      "duration_delta_minutes": 15, "max_quantity": 1,
      "exclusivity_group": null, "status": "ACTIVE"}
   ]}
]}

Rules:
- One entry per service, in the same order as the image.
- "name": the service name exactly as written.
- "base_price": the price in pounds as a string with no currency sign, e.g. \
"54.85". If a price is a range or "from" price, use the lowest number and \
mention the range in "description".
- "default_duration_minutes": a whole number of minutes, only if the image \
says how long it takes.
- "group": the heading or category the service is listed under, if any.
- "status": always "ACTIVE".
- "add_ons": optional extras listed with a service ("+ £10 for...", \
"add ... for £15"), on that service only. "price_delta" is the extra cost \
(negative for a discount) and "duration_delta_minutes" the extra time, 0 if \
not shown. Use "exclusivity_group" with the same short label for extras where \
only one can be chosen (e.g. sizes). Use [] when a service has none, and \
never list a service's add-ons as services of their own.
- Use null for anything you cannot read or that is not shown. Never invent \
prices, durations or services.
- Put anything useful that does not fit a field into "description".
- This business already has these services (skip them or use the same name): \
{{service_names}}
"""

APPOINTMENTS_EXAMPLE: dict[str, Any] = {
    "appointments": [
        {
            "service": "MOT Test",
            "start": "2026-11-03T09:30",
            "end": None,
            "status": "BOOKED",
            "employee_email": None,
            "customer": {
                "first_name": "Sam",
                "last_name": "Lee",
                "phone": "07123 456789",
                "email": None,
            },
            "notes": None,
        }
    ]
}

APPOINTMENTS_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Appointments import",
    "type": "object",
    "required": ["appointments"],
    "properties": {
        "appointments": {
            "type": "array",
            "maxItems": MAX_APPOINTMENTS,
            "items": {
                "type": "object",
                "required": ["service", "start", "customer"],
                "properties": {
                    "service": {
                        "type": "string",
                        "maxLength": 100,
                        "description": "An existing service name (case-insensitive).",
                    },
                    "start": {
                        "type": "string",
                        "description": "Local time in the business timezone, "
                        '"YYYY-MM-DDTHH:MM", or ISO 8601 with an explicit offset.',
                    },
                    "end": {
                        **_NULLABLE_STRING,
                        "description": "Optional; defaults to start + the service's duration.",
                    },
                    "status": {
                        **_NULLABLE_STRING,
                        "description": "One of the business's statuses; default BOOKED.",
                    },
                    "employee_email": {
                        **_NULLABLE_STRING,
                        "description": "A team member's login email; default the owner.",
                    },
                    "customer": {
                        "type": "object",
                        "required": ["first_name", "last_name"],
                        "properties": {
                            "first_name": {"type": "string", "minLength": 1, "maxLength": 100},
                            "last_name": {"type": "string", "minLength": 1, "maxLength": 100},
                            "phone": {"type": ["string", "null"], "maxLength": 40},
                            "email": {"type": ["string", "null"], "maxLength": 320},
                        },
                    },
                    "notes": {"type": ["string", "null"], "maxLength": 2000},
                },
            },
        }
    },
}

APPOINTMENTS_PROMPT = """\
Extract every booking from the attached image (a diary, calendar or booking \
list) and reply with ONLY a JSON object in exactly this format - no \
explanation, no markdown code fences:

{"appointments": [
  {"service": "...", "start": "2026-11-03T09:30", "end": null,
   "status": "BOOKED", "employee_email": null,
   "customer": {"first_name": "...", "last_name": "...", "phone": null, "email": null},
   "notes": null}
]}

Rules:
- One entry per booking, in the same order as the image.
- "service": must be one of these names, spelled exactly like this: \
{{service_names}}. If a booking matches none of them, use the closest one and \
say what the image actually said in "notes".
- "start" and "end": local times in {{timezone}}, written as \
"YYYY-MM-DDTHH:MM" with no timezone. Leave "end" null unless the image shows \
when the booking finishes.
- "status": one of {{statuses}}. Use "BOOKED" unless the image clearly shows \
otherwise (for example a cancelled booking).
- "customer": first and last name are required; split a full name into the \
two. Copy phone numbers and emails exactly as written.
- Use null for anything you cannot read. Never invent names, times or contact \
details.
- Anything else the image says about a booking (a vehicle, a request, a \
question's answer) goes in "notes" as plain text.
"""


def import_formats() -> dict[str, Any]:
    """Every import format, for the format endpoints of both UIs."""
    return {
        "services": {
            "example": SERVICES_EXAMPLE,
            "json_schema": SERVICES_SCHEMA,
            "ai_prompt_template": SERVICES_PROMPT,
        },
        "appointments": {
            "example": APPOINTMENTS_EXAMPLE,
            "json_schema": APPOINTMENTS_SCHEMA,
            "ai_prompt_template": APPOINTMENTS_PROMPT,
        },
        # What a business starts with - for filling the prompt before one
        # exists (the onboarding wizard). An existing business uses its own.
        "defaults": {
            "timezone": DEFAULT_BUSINESS_TIMEZONE,
            "statuses": list(DEFAULT_STATUS_KEYS),
        },
        "limits": {
            "max_body_bytes": MAX_BODY_BYTES,
            "max_services": MAX_SERVICES,
            "max_appointments": MAX_APPOINTMENTS,
        },
    }
