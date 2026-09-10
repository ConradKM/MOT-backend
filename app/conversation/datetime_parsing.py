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

_MONTHS = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sept": 9, "sep": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}  # fmt: skip
_MONTHS_ALT = "|".join(sorted(_MONTHS, key=len, reverse=True))
_ORD = r"(?:st|nd|rd|th)"

# "24 September", "24th Sept 2026", "September 24", "on the 24th of September"
_DAY_MONTH = re.compile(
    rf"\b(\d{{1,2}}){_ORD}?\s+(?:of\s+)?({_MONTHS_ALT})\b(?:\s+(\d{{4}}))?", re.IGNORECASE
)
_MONTH_DAY = re.compile(
    rf"\b({_MONTHS_ALT})\s+(\d{{1,2}}){_ORD}?\b(?:,?\s+(\d{{4}}))?", re.IGNORECASE
)
# ISO 2026-09-24
_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
# 24/09, 24-9-2026, 24.09.26  (day-first, UK)
_NUMERIC = re.compile(r"\b(\d{1,2})[/.\-](\d{1,2})(?:[/.\-](\d{2}|\d{4}))?\b")
# "the 24th" - day of month only. Requires the ordinal suffix: a bare number
# ("2") is left alone, since mid-conversation that's an option number or a
# time far more often than a day of the month.
_DAY_ONLY_ORD = re.compile(rf"\b(?:the\s+)?(\d{{1,2}}){_ORD}\b", re.IGNORECASE)


def _year_for(month: int, day: int, today: date) -> int:
    """Pick the sensible year for a day/month with no year given: this year
    if it hasn't passed yet, otherwise next year."""
    try:
        this_year = date(today.year, month, day)
    except ValueError:
        return today.year
    return today.year if this_year >= today else today.year + 1


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_explicit_date(text: str, *, now: datetime) -> date | None:
    """A concrete calendar date the customer stated outright - "24th September",
    "24/09", "2026-09-24", "the 24th". ``None`` if the text names no such
    date. A year that isn't given is inferred (this year, or next if the
    day/month has already passed); a date in the past is never returned.

    Kept separate from :func:`parse_date_phrase` so callers can tell "the
    customer gave an exact date" from "the customer said a weekday" - an
    explicit date must win over a weekday word in the same message
    ("Tuesday 24th September" is the 24th, not the next Tuesday).
    """
    if not text:
        return None
    today = now.date()

    m = _ISO.search(text)
    if m:
        # A full y-m-d is unambiguous - honour it or reject it outright, never
        # fall through and re-read its "-01-01" tail as a day/month.
        d = _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return d if d and d >= today else None

    for pat, day_grp, month_grp in ((_DAY_MONTH, 1, 2), (_MONTH_DAY, 2, 1)):
        m = pat.search(text)
        if m:
            month = _MONTHS[m.group(month_grp).lower()]
            day = int(m.group(day_grp))
            year = int(m.group(3)) if m.group(3) else _year_for(month, day, today)
            d = _safe_date(year, month, day)
            if d and d >= today:
                return d

    m = _NUMERIC.search(text)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        raw_year = m.group(3)
        if raw_year:
            year = int(raw_year) + 2000 if len(raw_year) == 2 else int(raw_year)
        else:
            year = _year_for(month, day, today) if month <= 12 else today.year
        d = _safe_date(year, month, day)
        if d and d >= today:
            return d

    m = _DAY_ONLY_ORD.search(text)
    if m:
        day = int(m.group(1))
        for offset in (0, 1, 2):  # this month, next, or the one after
            month = today.month + offset
            year = today.year + (month - 1) // 12
            month = (month - 1) % 12 + 1
            d = _safe_date(year, month, day)
            if d and d >= today:
                return d

    return None


def parse_date_phrase(text: str, *, now: datetime) -> date | None:
    """A concrete date from a phrase like "tomorrow", "Friday", "next Tuesday",
    "24th September", "24/09" - or ``None`` if nothing recognisable is in
    there. Always today-or-later. An explicit calendar date always wins over
    a weekday word in the same message.
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

    explicit = parse_explicit_date(text, now=now)
    if explicit is not None:
        return explicit

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
