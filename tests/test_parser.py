"""The deterministic 'add:' parser. 'now' is Monday 2026-09-28 10:00 ET."""
from datetime import datetime, timedelta

import pytest

from dispatch.clock import ET
from dispatch.parser import parse_add, parse_when

NOW = datetime(2026, 9, 28, 10, 0, tzinfo=ET)   # a Monday


@pytest.mark.parametrize("text,title,due", [
    ("add: email professor by Friday", "email professor", datetime(2026, 10, 2, 9, 0, tzinfo=ET)),
    ("add: call Sam tomorrow 3pm", "call Sam", datetime(2026, 9, 29, 15, 0, tzinfo=ET)),
    ("add: take out the trash tonight", "take out the trash", datetime(2026, 9, 28, 20, 0, tzinfo=ET)),
    ("add: submit the 1099 form 10/3", "submit the 1099 form", datetime(2026, 10, 3, 9, 0, tzinfo=ET)),
    ("add: renew license next Mon", "renew license", datetime(2026, 10, 5, 9, 0, tzinfo=ET)),
    ("add: stretch routine in 2 days", "stretch routine", datetime(2026, 9, 30, 9, 0, tzinfo=ET)),
    ("add: call mom 3pm", "call mom", datetime(2026, 9, 28, 15, 0, tzinfo=ET)),
    ("add: gym 9am", "gym", datetime(2026, 9, 29, 9, 0, tzinfo=ET)),        # 9am already passed
    ("ADD:  pay rent by fri 5:30 pm", "pay rent", datetime(2026, 10, 2, 17, 30, tzinfo=ET)),
    ("add: quiz prep on 10/3 at 7pm", "quiz prep", datetime(2026, 10, 3, 19, 0, tzinfo=ET)),
])
def test_due_phrases(text, title, due):
    got_title, got_due = parse_add(text, NOW)
    assert got_title == title
    assert got_due == due
    assert got_due.tzinfo is ET                       # always US Eastern, timezone-aware


def test_date_only_lands_at_9am_eastern():
    _, due = parse_add("add: return library book 10/3", NOW)
    assert (due.hour, due.minute) == (9, 0)
    assert due.utcoffset() == timedelta(hours=-4)     # EDT in October


def test_date_after_dst_change_uses_est():
    due = parse_when("11/2", NOW)
    assert due == datetime(2026, 11, 2, 9, 0, tzinfo=ET)
    assert due.utcoffset() == timedelta(hours=-5)


@pytest.mark.parametrize("text,title", [
    ("add: buy milk", "buy milk"),
    ("add: read chapter 3", "read chapter 3"),       # a bare number is not a time
    ("add: tomorrow", "tomorrow"),                  # never eat the whole title
])
def test_no_due_phrase(text, title):
    assert parse_add(text, NOW) == (title, None)


def test_same_weekday_rolls_forward_once_time_has_passed():
    friday_noon = datetime(2026, 10, 2, 12, 0, tzinfo=ET)
    assert parse_when("by Friday", friday_noon) == datetime(2026, 10, 9, 9, 0, tzinfo=ET)
    assert parse_when("friday 5pm", friday_noon) == datetime(2026, 10, 2, 17, 0, tzinfo=ET)


def test_garbage_is_none():
    for s in ("someday", "13/45", "next blah", "25:00", ""):
        assert parse_when(s, NOW) is None


# ---- Max's rule (2026-09-28): "next Wednesday" = the following calendar week's Wednesday;
# plain / "this" Wednesday = the nearest upcoming one. US weeks: Sunday starts the week.
@pytest.mark.parametrize("today,phrase,expected", [
    (datetime(2026, 10, 5, 9, 0, tzinfo=ET), "next wednesday", datetime(2026, 10, 14, 9, 0, tzinfo=ET)),  # Mon
    (datetime(2026, 10, 5, 9, 0, tzinfo=ET), "wednesday", datetime(2026, 10, 7, 9, 0, tzinfo=ET)),
    (datetime(2026, 10, 5, 9, 0, tzinfo=ET), "this wednesday", datetime(2026, 10, 7, 9, 0, tzinfo=ET)),
    (datetime(2026, 10, 8, 9, 0, tzinfo=ET), "next wednesday", datetime(2026, 10, 14, 9, 0, tzinfo=ET)),  # Thu
    (datetime(2026, 10, 8, 9, 0, tzinfo=ET), "wednesday", datetime(2026, 10, 14, 9, 0, tzinfo=ET)),
    (datetime(2026, 10, 10, 9, 0, tzinfo=ET), "next wednesday", datetime(2026, 10, 14, 9, 0, tzinfo=ET)),  # Sat
    (datetime(2026, 10, 11, 9, 0, tzinfo=ET), "wednesday", datetime(2026, 10, 14, 9, 0, tzinfo=ET)),  # Sun
    (datetime(2026, 10, 11, 9, 0, tzinfo=ET), "next wednesday", datetime(2026, 10, 21, 9, 0, tzinfo=ET)),
    (datetime(2026, 10, 7, 9, 0, tzinfo=ET), "next wed 3pm", datetime(2026, 10, 14, 15, 0, tzinfo=ET)),  # Wed
    (datetime(2026, 10, 5, 9, 0, tzinfo=ET), "next sunday", datetime(2026, 10, 11, 9, 0, tzinfo=ET)),
    (datetime(2026, 10, 11, 9, 0, tzinfo=ET), "next sunday", datetime(2026, 10, 18, 9, 0, tzinfo=ET)),
])
def test_next_weekday_is_the_following_calendar_week(today, phrase, expected):
    assert parse_when(phrase, today) == expected


def test_week_start_is_one_named_constant():
    from dispatch import parser
    assert parser.WEEK_START == 6          # Sunday (Python: Monday=0 ... Sunday=6)



# ---- review r2: parts of the day still parse after the next/this change -----------------------
@pytest.mark.parametrize("now,phrase,expected", [
    (datetime(2026, 10, 5, 9, 0, tzinfo=ET), "this afternoon", datetime(2026, 10, 5, 14, 0, tzinfo=ET)),
    (datetime(2026, 10, 5, 9, 0, tzinfo=ET), "afternoon", datetime(2026, 10, 5, 14, 0, tzinfo=ET)),
    (datetime(2026, 10, 5, 9, 0, tzinfo=ET), "this evening", datetime(2026, 10, 5, 18, 0, tzinfo=ET)),
    (datetime(2026, 10, 5, 7, 0, tzinfo=ET), "this morning", datetime(2026, 10, 5, 9, 0, tzinfo=ET)),
    (datetime(2026, 10, 5, 10, 30, tzinfo=ET), "this morning", datetime(2026, 10, 5, 11, 30, tzinfo=ET)),
    (datetime(2026, 10, 5, 15, 0, tzinfo=ET), "this afternoon", datetime(2026, 10, 5, 16, 0, tzinfo=ET)),
    (datetime(2026, 10, 5, 13, 0, tzinfo=ET), "this morning", None),                 # it's over
    (datetime(2026, 10, 5, 9, 0, tzinfo=ET), "tomorrow morning", datetime(2026, 10, 6, 9, 0, tzinfo=ET)),
    (datetime(2026, 10, 5, 9, 0, tzinfo=ET), "friday afternoon", datetime(2026, 10, 9, 14, 0, tzinfo=ET)),
    (datetime(2026, 10, 5, 9, 0, tzinfo=ET), "next wed evening", datetime(2026, 10, 14, 18, 0, tzinfo=ET)),
    (datetime(2026, 10, 5, 9, 0, tzinfo=ET), "this friday", datetime(2026, 10, 9, 9, 0, tzinfo=ET)),
    (datetime(2026, 10, 5, 9, 0, tzinfo=ET), "next week", None),
    (datetime(2026, 10, 5, 9, 0, tzinfo=ET), "this week", None),
])
def test_parts_of_the_day(now, phrase, expected):
    assert parse_when(phrase, now) == expected


def test_add_with_this_afternoon():
    title, due = parse_add("add: call Sam this afternoon", datetime(2026, 10, 5, 9, 0, tzinfo=ET))
    assert (title, due) == ("call Sam", datetime(2026, 10, 5, 14, 0, tzinfo=ET))
