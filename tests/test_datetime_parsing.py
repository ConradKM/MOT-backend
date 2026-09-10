"""Unit tests for app/conversation/datetime_parsing.py - especially the
explicit calendar-date parsing added after the +44 7925 392354 conversation,
where "book for the 24th September" was silently turned into the next Tuesday
(15 September)."""

from datetime import UTC, date, datetime

import pytest

from app.conversation.datetime_parsing import parse_date_phrase, parse_explicit_date

# A fixed "now" so the tests never depend on the real calendar. A Thursday.
NOW = datetime(2026, 9, 10, 9, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "text",
    [
        "the 24th september",
        "24th september",
        "24 september",
        "24 sept",
        "september 24th",
        "on the 24th of September please",
        "can i book for the 24th september",
        "2026-09-24",
        "24/09",
        "24/09/2026",
        "24-9-26",
    ],
)
def test_explicit_24_september_is_kept_as_24_september(text):
    assert parse_date_phrase(text, now=NOW) == date(2026, 9, 24)


def test_weekday_plus_explicit_date_uses_the_explicit_date_not_the_weekday():
    # The exact failure from the real conversation: "Tuesday 24th September"
    # must be the 24th, never "the next Tuesday" (which would be 15 Sept).
    assert parse_date_phrase("Tuesday 24th september", now=NOW) == date(2026, 9, 24)
    assert parse_date_phrase("tues 24 sep", now=NOW) == date(2026, 9, 24)


def test_bare_weekday_still_works_when_no_explicit_date_is_present():
    # 2026-09-10 is a Thursday; the next Tuesday is the 15th.
    assert parse_date_phrase("tuesday", now=NOW) == date(2026, 9, 15)
    assert parse_date_phrase("next tuesday", now=NOW) == date(2026, 9, 15)


def test_relative_words_unchanged():
    assert parse_date_phrase("tomorrow", now=NOW) == date(2026, 9, 11)
    assert parse_date_phrase("day after tomorrow", now=NOW) == date(2026, 9, 12)
    assert parse_date_phrase("today", now=NOW) == date(2026, 9, 10)


def test_year_is_inferred_forward_when_the_month_has_already_passed():
    # "3rd January" said in September means next January.
    assert parse_explicit_date("3rd january", now=NOW) == date(2027, 1, 3)
    # "24 December" is still this year.
    assert parse_explicit_date("24 december", now=NOW) == date(2026, 12, 24)


def test_day_only_reply_is_this_month_or_the_next_occurrence():
    assert parse_explicit_date("the 24th", now=NOW) == date(2026, 9, 24)
    # A day-of-month that has already passed rolls to next month.
    assert parse_explicit_date("the 3rd", now=NOW) == date(2026, 10, 3)


def test_a_bare_number_is_not_treated_as_a_day_of_month():
    # Mid-conversation "2" is an option number or a time, not "the 2nd".
    assert parse_explicit_date("2", now=NOW) is None
    assert parse_explicit_date("24", now=NOW) is None


def test_a_yearless_month_day_just_before_today_rolls_to_next_year():
    # "1st September" said on the 10th almost certainly means next year, not
    # eleven days ago - so it rolls forward rather than being dropped.
    assert parse_explicit_date("1st september", now=NOW) == date(2027, 9, 1)


def test_an_explicitly_past_date_is_rejected():
    assert parse_explicit_date("2020-01-01", now=NOW) is None
    assert parse_explicit_date("01/01/2020", now=NOW) is None


def test_no_date_returns_none():
    assert parse_date_phrase("what are your prices", now=NOW) is None
    assert parse_date_phrase("", now=NOW) is None
