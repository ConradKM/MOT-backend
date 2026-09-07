"""Deterministic natural-language date/time phrase parsing.

Resolves *preferences* only ("Tuesday morning", "around 3") - never a
booking decision by itself. Every caller in workflows.py takes whatever this
module returns and still runs it through the real availability service
(app/public_booking/availability.py) before offering or accepting a slot;
see that package's module docstring for why this split matters.

Timezone: matches the rest of the codebase (see
app/public_booking/availability.py's module docstring) - garages are treated
as operating on plain UK wall-clock time with no timezone conversion, so
"9am" always means ``time(9, 0)`` and dates are compared against
``datetime.now(UTC).date()``. Revisiting this needs a per-garage timezone
column, same as the availability engine this sits on top of.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

_WEEKDAYS = {
    "monday": 0,
    "mon": 0,
    "tuesday": 1,
    "tue": 1,
    "tues": 1,
    "wednesday": 2,
    "wed": 2,
    "thursday": 3,
    "thu": 3,
    "thurs": 3,
    "friday": 4,
    "fri": 4,
    "saturday": 5,
    "sat": 5,
    "sunday": 6,
    "sun": 6,
}
_WEEKDAY_PATTERN = re.compile(r"\b(next\s+)?(" + "|".join(_WEEKDAYS) + r")\b", re.IGNORECASE)


def parse_date_phrase(text: str, *, now: datetime) -> date | None:
    """A concrete date from a phrase like "tomorrow", "Friday", "next Tuesday" -
    or ``None`` if nothing recognisable is in there. Always today-or-later.
    """
    if not text:
        return None
    lowered = text.lower()
    today = now.date()

    if "day after tomorrow" in lowered:
        return today + timedelta(days=2)
    if "tomorrow" in lowered:
        return today + timedelta(days=1)
    if "today" in lowered or "this afternoon" in lowered or "this morning" in lowered:
        return today

    match = _WEEKDAY_PATTERN.search(lowered)
    if match:
        target_weekday = _WEEKDAYS[match.group(2).lower()]
        days_ahead = (target_weekday - today.weekday()) % 7
        if days_ahead == 0:
            # Today's own weekday named by itself ("Monday" when today
            # already is Monday), or an explicit "next <weekday>" - either
            # way they mean the next occurrence, a week out. Literal "today"
            # is handled above, before this branch is ever reached, so a
            # bare weekday name that matches today can't mean "later today".
            days_ahead = 7
        return today + timedelta(days=days_ahead)

    if "next week" in lowered:
        return today + timedelta(days=7)

    return None


# (label, earliest, latest) - deliberately generic clock windows, intersected
# with whatever the garage's *real* opening hours and slots are by
# workflows.py; these never claim the business is open at these times.
_TIME_WINDOWS: tuple[tuple[str, time, time], ...] = (
    ("first thing", time(0, 0), time(10, 0)),
    ("early morning", time(0, 0), time(10, 0)),
    ("late afternoon", time(15, 0), time(18, 0)),
    ("after lunch", time(13, 0), time(17, 30)),
    ("before lunch", time(0, 0), time(12, 0)),
    ("late", time(15, 30), time(20, 0)),
    ("morning", time(0, 0), time(12, 0)),
    ("afternoon", time(12, 0), time(17, 30)),
    ("evening", time(17, 0), time(21, 0)),
)

_ARBITRARY_DATE = date(2000, 1, 1)

_AROUND_PATTERN = re.compile(
    r"\b(around|about|approx(?:imately)?)\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b",
    re.IGNORECASE,
)


def parse_time_window_phrase(text: str) -> tuple[time, time] | None:
    """A rough ``(earliest, latest)`` window from a vague time-of-day phrase,
    or ``None`` if the text doesn't hint at one. Checked before
    :func:`parse_exact_time_phrase`'s stricter patterns."""
    if not text:
        return None
    lowered = text.lower()

    around = _AROUND_PATTERN.search(lowered)
    if around:
        hour = int(around.group(2))
        meridiem = around.group(4)
        hour = _resolve_hour(hour, meridiem)
        # The date half is thrown away below - only used so +/-45 minutes
        # can cross midnight cleanly before taking .time() back off it -
        # so an arbitrary fixed date, never a real "today", is deliberate.
        centre = datetime.combine(_ARBITRARY_DATE, time(hour, int(around.group(3) or 0)))
        start = (centre - timedelta(minutes=45)).time()
        end = (centre + timedelta(minutes=45)).time()
        return (start, end)

    for label, start, end in _TIME_WINDOWS:
        if label in lowered:
            return (start, end)

    return None


_EXACT_TIME_PATTERNS = (
    re.compile(r"\b(\d{1,2})[:.](\d{2})\s*(am|pm)?\b", re.IGNORECASE),
    re.compile(r"\b(\d{1,2})\s*(am|pm)\b", re.IGNORECASE),
    re.compile(r"\bhalf\s+past\s+(\d{1,2})\b", re.IGNORECASE),
    re.compile(r"\bhalf\s+(\d{1,2})\b", re.IGNORECASE),
    re.compile(r"\bquarter\s+past\s+(\d{1,2})\b", re.IGNORECASE),
    re.compile(r"\bquarter\s+to\s+(\d{1,2})\b", re.IGNORECASE),
)


def _resolve_hour(hour: int, meridiem: str | None) -> int:
    """24-hour form for an ambiguous spoken hour. An explicit am/pm always
    wins; otherwise a garage-hours heuristic (1-7 leans afternoon/evening,
    since no garage opens at 1am) - genuinely ambiguous, but a safe default
    given every offered slot is re-validated against real opening hours
    anyway."""
    if meridiem:
        meridiem = meridiem.lower()
        if meridiem == "pm" and hour != 12:
            return hour + 12
        if meridiem == "am" and hour == 12:
            return 0
        return hour
    if 1 <= hour <= 7:
        return hour + 12
    return hour % 24


def parse_exact_time_phrase(text: str) -> time | None:
    """A specific clock time from a phrase like "10:30", "10am", "half 10",
    "quarter past 2" - or ``None``. Tried after
    :func:`parse_time_window_phrase` fails to find a vaguer window."""
    if not text:
        return None
    lowered = text.lower()

    m = _EXACT_TIME_PATTERNS[0].search(lowered)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        return time(_resolve_hour(hour, m.group(3)) % 24, minute)

    m = _EXACT_TIME_PATTERNS[1].search(lowered)
    if m:
        hour = int(m.group(1))
        return time(_resolve_hour(hour, m.group(2)) % 24, 0)

    m = _EXACT_TIME_PATTERNS[2].search(lowered)  # "half past X" = X:30
    if m:
        return time(_resolve_hour(int(m.group(1)), None) % 24, 30)

    m = _EXACT_TIME_PATTERNS[3].search(lowered)  # "half X" (colloquial UK) = X:30
    if m:
        return time(_resolve_hour(int(m.group(1)), None) % 24, 30)

    m = _EXACT_TIME_PATTERNS[4].search(lowered)  # "quarter past X" = X:15
    if m:
        return time(_resolve_hour(int(m.group(1)), None) % 24, 15)

    m = _EXACT_TIME_PATTERNS[5].search(lowered)  # "quarter to X" = (X-1):45
    if m:
        hour = _resolve_hour(int(m.group(1)), None) % 24
        return time((hour - 1) % 24, 45)

    return None
