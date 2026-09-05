"""Safe fuzzy matching of a customer's free-text service request against a
*specific garage's own* appointment types (never a hard-coded MOT/Service/
Diagnostic list - see Part 5 of the brief: a hairdresser's "Haircut"/"Beard
Trim" types must match exactly the same way).

Deliberately conservative: a match is only ever auto-applied when it is the
*unique* best match for this garage's current type list - see
:func:`match_appointment_type`'s docstring for why that, not a fixed
confidence score, is the right test. Anything less certain comes back as
candidates for the workflow to ask the customer to disambiguate ("Did you
mean Full Service or Interim Service?"), never a silent guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.models.appointments.appointment_type import GarageAppointmentType

_MAX_CANDIDATES = 3

_STOPWORDS = {
    "a", "an", "the", "my", "for", "please", "want", "need", "book", "booking",
    "to", "get", "have", "car", "vehicle", "in", "on", "of", "i", "is",
}


def _tokenize(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {w for w in words if w not in _STOPWORDS}


@dataclass
class AppointmentTypeMatch:
    matched: GarageAppointmentType | None
    candidates: list[GarageAppointmentType] = field(default_factory=list)

    @property
    def is_confident(self) -> bool:
        return self.matched is not None

    @property
    def is_ambiguous(self) -> bool:
        return self.matched is None and bool(self.candidates)


def match_appointment_type(garage, text: str) -> AppointmentTypeMatch:
    """Resolve ``text`` against this garage's own ACTIVE appointment types.

    Two safe ways to auto-select, checked in order:

    1. The customer's text contains a type's full name verbatim ("full
       service please" contains "full service") - unambiguous no matter how
       many other types exist.
    2. Otherwise, whichever type shares the *most* of its own name-words
       with what the customer said, but only if it is the single, strict
       top scorer. Raw overlap count is used rather than a ratio/percentage
       deliberately: a customer who says "MOT" has said 1 of "MOT Test"'s 2
       words, but that word fully identifies it (nothing else here is
       called "MOT"), so it must still win outright. A customer who says
       "service" has also said 1 word, but if both "Full Service" and
       "Interim Service" exist, both score equally on that same word - a
       genuine tie, not a confident match - so neither is picked; both come
       back as candidates instead.
    """
    active_types = [t for t in garage.appointment_types if t.status == "ACTIVE"]
    if not active_types:
        return AppointmentTypeMatch(matched=None, candidates=[])

    customer_tokens = _tokenize(text)
    if not customer_tokens:
        return AppointmentTypeMatch(matched=None, candidates=[])
    customer_text_lower = text.lower()

    for appointment_type in active_types:
        name_lower = appointment_type.name.lower()
        if re.search(r"\b" + re.escape(name_lower) + r"\b", customer_text_lower):
            return AppointmentTypeMatch(matched=appointment_type, candidates=[])

    scored = [
        (t, len(_tokenize(t.name) & customer_tokens))
        for t in active_types
    ]
    scored = [(t, score) for t, score in scored if score > 0]
    if not scored:
        return AppointmentTypeMatch(matched=None, candidates=[])

    scored.sort(key=lambda pair: -pair[1])
    top_score = scored[0][1]
    top_scorers = [t for t, score in scored if score == top_score]

    if len(top_scorers) == 1:
        return AppointmentTypeMatch(matched=top_scorers[0], candidates=[])

    return AppointmentTypeMatch(matched=None, candidates=top_scorers[:_MAX_CANDIDATES])
