"""Customer-intent detection.

``IntentResolver`` is a swappable strategy - today's ``RuleBasedIntentResolver``
is pure keyword/phrase matching, deterministic and needs no external service.
A future ``LLMIntentResolver`` could implement the same interface (Part 32 of
the brief); nothing in engine.py/workflows.py would need to change, because
both only ever see one of the plain intent strings below - never raw model
output.
"""

from __future__ import annotations

import re
from typing import Protocol

CREATE_BOOKING = "CREATE_BOOKING"
CHECK_AVAILABILITY = "CHECK_AVAILABILITY"
CHECK_APPOINTMENT = "CHECK_APPOINTMENT"
RESCHEDULE_APPOINTMENT = "RESCHEDULE_APPOINTMENT"
CANCEL_APPOINTMENT = "CANCEL_APPOINTMENT"
APPOINTMENT_PRICE_QUERY = "APPOINTMENT_PRICE_QUERY"
APPOINTMENT_TYPE_QUERY = "APPOINTMENT_TYPE_QUERY"
BUSINESS_HOURS_QUERY = "BUSINESS_HOURS_QUERY"
BUSINESS_LOCATION_QUERY = "BUSINESS_LOCATION_QUERY"
MOT_EXPIRY_QUERY = "MOT_EXPIRY_QUERY"
CUSTOMER_DETAILS_QUERY = "CUSTOMER_DETAILS_QUERY"
CALLBACK_REQUEST = "CALLBACK_REQUEST"
SPEAK_TO_HUMAN = "SPEAK_TO_HUMAN"
GENERAL_QUERY = "GENERAL_QUERY"
UNKNOWN = "UNKNOWN"

INTENTS = (
    CREATE_BOOKING,
    CHECK_AVAILABILITY,
    CHECK_APPOINTMENT,
    RESCHEDULE_APPOINTMENT,
    CANCEL_APPOINTMENT,
    APPOINTMENT_PRICE_QUERY,
    APPOINTMENT_TYPE_QUERY,
    BUSINESS_HOURS_QUERY,
    BUSINESS_LOCATION_QUERY,
    MOT_EXPIRY_QUERY,
    CUSTOMER_DETAILS_QUERY,
    CALLBACK_REQUEST,
    SPEAK_TO_HUMAN,
    GENERAL_QUERY,
    UNKNOWN,
)

# Intents that should always win, even mid-workflow - a customer can always
# ask for a human or a callback instead of answering the current question.
# Checked by engine.py before it tries to interpret a reply as a slot fill.
INTERRUPT_INTENTS = (SPEAK_TO_HUMAN, CALLBACK_REQUEST, CANCEL_APPOINTMENT)


class IntentResolver(Protocol):
    def resolve(self, text: str) -> str: ...


def _phrase_pattern(phrases: tuple[str, ...]) -> re.Pattern:
    return re.compile("|".join(re.escape(p) for p in phrases), re.IGNORECASE)


# Ordered most-specific-first: the first matching rule wins, so (e.g.)
# "when is my mot due" hits MOT_EXPIRY_QUERY before the generic "mot" keyword
# in CREATE_BOOKING ever gets a chance to misfire.
_RULES: tuple[tuple[str, re.Pattern], ...] = (
    (
        SPEAK_TO_HUMAN,
        _phrase_pattern(
            (
                "speak to a person",
                "speak to someone",
                "speak to a human",
                "talk to a person",
                "talk to someone",
                "talk to a human",
                "real person",
                "human please",
                "customer service",
                "complain",
                "complaint",
                "not happy",
                "unhappy with",
                "speak to a manager",
                "speak to staff",
                "someone from the garage",
            )
        ),
    ),
    (
        CALLBACK_REQUEST,
        _phrase_pattern(
            (
                "call me back",
                "call me please",
                "ring me back",
                "ring me please",
                "can someone call",
                "can you call me",
                "give me a call",
                "phone me back",
                "phone me please",
                "someone to call me",
            )
        ),
    ),
    (
        CANCEL_APPOINTMENT,
        _phrase_pattern(
            (
                "cancel my",
                "cancel the",
                "cancel appointment",
                "cancel booking",
                "no longer need",
                "don't need the appointment",
            )
        ),
    ),
    (
        RESCHEDULE_APPOINTMENT,
        _phrase_pattern(
            (
                "reschedule",
                "move my appointment",
                "move my booking",
                "change my appointment",
                "change my booking",
                "different day",
                "different time",
                "push back my",
                "bring forward my",
            )
        ),
    ),
    (
        MOT_EXPIRY_QUERY,
        _phrase_pattern(
            (
                "mot due",
                "mot expire",
                "mot expiry",
                "when is my mot",
                "mot run out",
                "mot renewal",
            )
        ),
    ),
    (
        CHECK_APPOINTMENT,
        _phrase_pattern(
            (
                "when is my appointment",
                "when is my booking",
                "what time is my",
                "do i have anything booked",
                "do i have an appointment",
                "have i got an appointment",
                "have i got a booking",
                "check my appointment",
                "check my booking",
                "my next appointment",
            )
        ),
    ),
    (
        APPOINTMENT_PRICE_QUERY,
        _phrase_pattern(
            (
                "how much is",
                "how much does",
                "how much for",
                "what does it cost",
                "what's the cost",
                "what is the cost",
                "price of",
                "price for",
                "cost of",
                "cost for",
            )
        ),
    ),
    (
        BUSINESS_HOURS_QUERY,
        _phrase_pattern(
            (
                "opening hours",
                "what time do you open",
                "what time do you close",
                "are you open",
                "when are you open",
                "your hours",
            )
        ),
    ),
    (
        BUSINESS_LOCATION_QUERY,
        _phrase_pattern(
            (
                "where are you",
                "your address",
                "what's your address",
                "your location",
                "how do i find you",
                "postcode",
            )
        ),
    ),
    (
        APPOINTMENT_TYPE_QUERY,
        _phrase_pattern(
            (
                "what services",
                "what do you offer",
                "what can you do",
                "do you do mots",
                "do you do services",
                "what appointments",
            )
        ),
    ),
    (
        CUSTOMER_DETAILS_QUERY,
        _phrase_pattern(
            (
                "my details",
                "my account",
                "my information",
                "what car do i have",
                "what vehicle do i have",
                "my vehicles",
            )
        ),
    ),
    (
        CHECK_AVAILABILITY,
        _phrase_pattern(
            (
                "what slots",
                "what times",
                "what availability",
                "when can i come",
                "any availability",
                "are you free",
            )
        ),
    ),
    (
        CREATE_BOOKING,
        _phrase_pattern(
            (
                "book",
                "booking",
                "appointment",
                "mot",
                "service",
                "diagnostic",
                "diagnostics",
                "get my car in",
                "bring my car in",
                "bring the car in",
            )
        ),
    ),
)

_QUESTION_HINT = re.compile(r"\?|^(what|when|where|how|can|could|do you|is there)\b", re.IGNORECASE)


class RuleBasedIntentResolver:
    """Deterministic keyword/phrase matching - no external service, no
    randomness, the same input always resolves to the same intent."""

    def resolve(self, text: str) -> str:
        if not text or not text.strip():
            return UNKNOWN

        for intent, pattern in _RULES:
            if pattern.search(text):
                return intent

        return GENERAL_QUERY if _QUESTION_HINT.search(text) else UNKNOWN
