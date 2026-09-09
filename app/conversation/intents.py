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
# A message that's only a hello / "what can you do" / "help" - answered with a
# short capability intro rather than "sorry, didn't follow that".
GREETING = "GREETING"
# "thanks" / "cheers" / "that's all" on their own - a polite acknowledgement
# and close.
SMALL_TALK = "SMALL_TALK"
# Navigation / flow-control - a customer steering the conversation itself
# rather than answering the current question. Handled by engine._dispatch
# *before* the message is ever passed to a workflow step (so "back to the
# beginning" is never fed to appointment matching).
RESET_FLOW = "RESET_FLOW"  # "start again", "back to the beginning", "restart"
ABANDON_FLOW = "ABANDON_FLOW"  # "cancel that", "never mind", "forget it", "stop"
GO_BACK = "GO_BACK"  # "go back", "back a step", "undo that"
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
    GREETING,
    SMALL_TALK,
    RESET_FLOW,
    ABANDON_FLOW,
    GO_BACK,
    GENERAL_QUERY,
    UNKNOWN,
)

# Steer-the-conversation intents (engine._dispatch handles these before a
# workflow step ever sees the message).
NAV_INTENTS = (RESET_FLOW, ABANDON_FLOW, GO_BACK)

# Pure-FAQ intents whose one-shot answer is safe to (a) combine with others
# in a single reply and (b) answer mid-workflow without disturbing the
# paused flow.
FAQ_INTENTS = (
    BUSINESS_HOURS_QUERY,
    BUSINESS_LOCATION_QUERY,
    APPOINTMENT_PRICE_QUERY,
    APPOINTMENT_TYPE_QUERY,
)

# Intents that should always win, even mid-workflow - a customer part-way
# through booking can still say "actually I need to cancel/move my existing
# appointment", ask for a human, or ask for a callback, and be taken there
# instead of having the message parsed as an answer to the current question.
# Checked by engine.py before it tries to interpret a reply as a slot fill.
# The phrases for these intents are specific enough ("cancel my", "reschedule",
# "move my appointment", "can't make it") that an ordinary slot answer - a
# day, a time, a service name, "yes" - never trips them.
INTERRUPT_INTENTS = (
    SPEAK_TO_HUMAN,
    CALLBACK_REQUEST,
    CANCEL_APPOINTMENT,
    RESCHEDULE_APPOINTMENT,
)


class IntentResolver(Protocol):
    def resolve(self, text: str) -> str: ...


def _phrase_pattern(phrases: tuple[str, ...]) -> re.Pattern:
    return re.compile("|".join(re.escape(p) for p in phrases), re.IGNORECASE)


# Common WhatsApp shorthand -> the word the rules below already match on.
# Deliberately small and booking-vocabulary-focused: whole-word only, so it
# never mangles a name, a registration, or ordinary prose.
_SHORTHAND = {
    "u": "you",
    "ur": "your",
    "pls": "please",
    "plz": "please",
    "thx": "thanks",
    "thnx": "thanks",
    "ty": "thanks",
    "appt": "appointment",
    "appts": "appointments",
    "apt": "appointment",
    "resched": "reschedule",
    "wanna": "want to",
    "gonna": "going to",
    "gotta": "got to",
    "tmrw": "tomorrow",
    "tmr": "tomorrow",
    "tomo": "tomorrow",
    "asap": "as soon as possible",
    "avail": "availability",
    "info": "information",
}
_SHORTHAND_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in _SHORTHAND) + r")\b", re.IGNORECASE
)


def _normalise(text: str) -> str:
    """Lower-case, collapse whitespace, drop surrounding quotes/trailing
    punctuation, and expand the shorthand map - so "Can u book me an appt
    for tmrw??" matches the same rules as "can you book me an appointment
    for tomorrow". Intent detection only; workflows still see the raw text
    for names / registrations / free text."""
    lowered = " ".join(text.lower().split())
    lowered = lowered.strip("\"'“”‘’ ").rstrip("!.?, ")
    return _SHORTHAND_RE.sub(lambda m: _SHORTHAND[m.group(1).lower()], lowered)


# A message that is *only* a greeting (optionally with "there"/name) - or a
# bare help/menu ask.
_GREETING_RE = re.compile(
    r"^(hi|hello|hey|heya|hiya|yo|hallo|howdy|"
    r"good\s+(morning|afternoon|evening)|morning|afternoon|evening)"
    r"([\s,]+(there|team|guys|folks))?$",
    re.IGNORECASE,
)
_HELP_RE = _phrase_pattern(
    (
        "what can you do",
        "what can you help",
        "how does this work",
        "how do i use this",
        "what do you do",
        "help me",
        "need help",
        "show me options",
        "what are my options",
    )
)
_HELP_WORDS = {"help", "menu", "options", "start"}
_SMALL_TALK_RE = re.compile(
    r"^(thanks|thank you|thankyou|thanks a lot|thanks very much|thank you very much|"
    r"many thanks|cheers|ta|nice one|great thanks|ok thanks|okay thanks|perfect thanks|"
    r"brilliant|lovely|great stuff|"
    r"bye|goodbye|see you|see ya|good day|"
    r"that'?s (all|it|everything)|that is all|nothing else|"
    r"no (that'?s|thats) (all|it|everything)|all good|we'?re good|i'?m good|no thanks)"
    r"[\s,]*(thanks|thank you|cheers|bye)?$",
    re.IGNORECASE,
)

# Flow-control phrases. Anchored to the whole (normalised) message so
# "cancel that" is ABANDON_FLOW while "cancel my appointment on Friday" still
# falls through to CANCEL_APPOINTMENT below. Checked before _RULES so a
# navigation phrase is never mistaken for a service name or a date.
# A little conversational filler is allowed in front of any of these.
_NAV_PREFIX = r"^(please\s+|ok(ay)?,?\s+|right,?\s+|actually,?\s+|erm,?\s+|hmm,?\s+|no,?\s+)*"

_RESET_RE = re.compile(
    _NAV_PREFIX + r"("
    r"start (over|again|from scratch|from the start)|"
    r"restart|reset|start from scratch|start from the beginning|"
    r"back to (the )?(start|beginning|top|very beginning)|"
    r"(go )?right back to the start|from the (start|top|beginning)|begin again|"
    r"let'?s start (over|again)|scrap (all|everything) and start again"
    r")$",
    re.IGNORECASE,
)
_ABANDON_RE = re.compile(
    _NAV_PREFIX + r"("
    r"never ?mind|forget (it|that|about it|this)|forget the whole thing|"
    r"cancel (that|this|it)|stop( it| this)?|drop it|leave it|"
    r"not now|not right now|not anymore|don'?t bother|scrap (that|it|this)|"
    r"i (don'?t|do not) want to (do this|book|continue)( anymore)?|"
    r"changed my mind"
    r")$",
    re.IGNORECASE,
)
_GO_BACK_RE = re.compile(
    _NAV_PREFIX + r"("
    r"go back( a step| one step)?|back a step|back one step|one step back|"
    r"previous( step| question)?|undo( that)?|go back to the (last|previous) (step|question)"
    r")$",
    re.IGNORECASE,
)


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
                "cancel that appointment",
                "cancel that booking",
                "cancel appointment",
                "cancel booking",
                "cancel it please",
                "need to cancel",
                "want to cancel",
                "have to cancel",
                "got to cancel",
                "like to cancel",
                "wish to cancel",
                "please cancel",
                "no longer need",
                "don't need the appointment",
                "don't need my appointment",
                "don't want the appointment",
                "call off my appointment",
            )
        ),
    ),
    (
        RESCHEDULE_APPOINTMENT,
        _phrase_pattern(
            (
                "reschedule",
                "re-schedule",
                "rearrange",
                "move my appointment",
                "move my booking",
                "move the appointment",
                "move it to",
                "change my appointment",
                "change my booking",
                "change the appointment",
                "change the time of my",
                "change the day of my",
                "push back my",
                "bring forward my",
                "need to move",
                "want to move",
                "need to change my appointment",
                "need to change my booking",
                "can't make it",
                "cant make it",
                "can't make my appointment",
                "can't make tomorrow",
                "can't make the appointment",
                "cannot make it",
                "cannot make my appointment",
                "can't come in",
                "cannot come in",
                "won't be able to make",
                "not able to make",
                "unable to make",
                "won't make my appointment",
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
                "how much would",
                "how much will",
                "how much",
                "what does it cost",
                "what's the cost",
                "what is the cost",
                "price of",
                "price for",
                "prices",
                "pricing",
                "price list",
                "cost of",
                "cost for",
                "what do you charge",
                "how much do you charge",
            )
        ),
    ),
    (
        BUSINESS_HOURS_QUERY,
        _phrase_pattern(
            (
                "opening hours",
                "opening time",
                "what time do you open",
                "what time do you close",
                "when do you open",
                "when do you close",
                "how late are you open",
                "are you open",
                "you open today",
                "you open tomorrow",
                "open on saturday",
                "open on sunday",
                "when are you open",
                "your hours",
                "what are your hours",
                "business hours",
            )
        ),
    ),
    (
        BUSINESS_LOCATION_QUERY,
        _phrase_pattern(
            (
                "where are you",
                "where you are",
                "where you're",
                "where's your",
                "where is your",
                "where are you based",
                "where you based",
                "address",
                "your location",
                "where's it",
                "located",
                "how do i find you",
                "how to find you",
                "directions to you",
                "postcode",
            )
        ),
    ),
    (
        APPOINTMENT_TYPE_QUERY,
        _phrase_pattern(
            (
                "what services",
                "which services",
                "what services do you",
                "what do you offer",
                "what can you do",
                "do you do mots",
                "do you do services",
                "what appointments",
                "types of appointment",
                "kind of appointments",
                "list of services",
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
                "get booked in",
                "get it booked",
                "come in for",
                "sort out my",
                "look at my car",
                "get my car looked at",
                "need my car seen",
            )
        ),
    ),
)

_QUESTION_HINT = re.compile(r"\?|^(what|when|where|how|can|could|do you|is there)\b", re.IGNORECASE)

# The pattern for a given intent, for detect_faq_intents() below.
_RULE_BY_INTENT: dict[str, re.Pattern] = dict(_RULES)


def detect_faq_intents(text: str) -> list[str]:
    """Every pure-FAQ intent (`FAQ_INTENTS`) whose pattern the message
    matches, in a stable order - so "prices, opening hours and address"
    comes back as three intents to answer together, not one picked and the
    rest dropped. Empty when the message asks nothing FAQ-like."""
    normalised = _normalise(text)
    return [i for i in FAQ_INTENTS if _RULE_BY_INTENT[i].search(normalised)]


class RuleBasedIntentResolver:
    """Deterministic keyword/phrase matching - no external service, no
    randomness, the same input always resolves to the same intent."""

    def resolve(self, text: str) -> str:
        if not text or not text.strip():
            return UNKNOWN

        normalised = _normalise(text)
        if not normalised:
            return UNKNOWN

        # Steering the conversation itself always wins - and "back to the
        # beginning" must never reach a workflow's slot parser.
        if _RESET_RE.match(normalised):
            return RESET_FLOW
        if _ABANDON_RE.match(normalised):
            return ABANDON_FLOW
        if _GO_BACK_RE.match(normalised):
            return GO_BACK

        for intent, pattern in _RULES:
            if pattern.search(normalised):
                return intent

        # Only reached when nothing actionable matched: a bare hello, a
        # "help"/"menu", or a thanks/goodbye gets a friendly answer instead
        # of the generic "didn't follow that". A greeting *with* a request
        # ("hi, can I book an MOT") never lands here - the rules above catch
        # the request first.
        if (
            _GREETING_RE.match(normalised)
            or _HELP_RE.search(normalised)
            or normalised in _HELP_WORDS
        ):
            return GREETING
        if _SMALL_TALK_RE.match(normalised):
            return SMALL_TALK

        looks_like_question = "?" in text or bool(_QUESTION_HINT.search(normalised))
        return GENERAL_QUERY if looks_like_question else UNKNOWN
