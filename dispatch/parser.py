"""Deterministic due-date parser for '#inbox' capture. No AI involved.

    parse_add("add: email professor by Friday", now) -> ("email professor", Fri 09:00 ET)
    parse_add("add: call Sam tomorrow 3pm", now)    -> ("call Sam", tomorrow 15:00 ET)

Understood phrases (optionally led by 'by', 'on', 'due', 'at', 'before'):
    tonight | today [time] | tomorrow [time] | [this|next] <weekday> [time] | M/D[/YYYY] [time]
    (<weekday> / this <weekday> = the nearest upcoming one; next <weekday> = the one in the
    following calendar week, weeks starting Sunday - see WEEK_START)
    in N minutes|hours|days|weeks | <time> alone (3pm, 3:30pm, 15:00)
A phrase with a date but no time lands at 09:00 ET.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

from .clock import ET

DEFAULT_TIME = time(9, 0)
# First day of a calendar week, Python numbering (Monday=0 ... Sunday=6). US calendars start on
# Sunday. Only "next <weekday>" uses it. Change to 0 if weeks should start on Monday.
WEEK_START = 6
TONIGHT_TIME = time(20, 0)

_WEEKDAYS = {"mon": 0, "monday": 0, "tue": 1, "tues": 1, "tuesday": 1, "wed": 2, "weds": 2,
             "wednesday": 2, "thu": 3, "thur": 3, "thurs": 3, "thursday": 3, "fri": 4,
             "friday": 4, "sat": 5, "saturday": 5, "sun": 6, "sunday": 6}
_LEAD = re.compile(r"^(?:by|on|due|at|before)\s+")
_TIME = re.compile(r"^(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$")
_REL = re.compile(r"^in\s+(\d+)\s*(m|min|mins|minutes?|h|hr|hrs|hours?|d|days?|w|wks?|weeks?)$")
_DATE = re.compile(r"^(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?$")


def _parse_time(s: str) -> time | None:
    m = _TIME.match(s.strip())
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap is None and m.group(2) is None:
        return None          # a bare number like "3" is too ambiguous to be a time
    if mi > 59:
        return None
    if ap:
        if not 1 <= h <= 12:
            return None
        h = (h % 12) + (12 if ap == "pm" else 0)
    elif h > 23:
        return None
    return time(h, mi)


def _at(d: date, t: time) -> datetime:
    return datetime.combine(d, t, tzinfo=ET)


# Parts of the day: "this afternoon", "tomorrow morning", "friday evening".
PARTS = {"morning": time(9, 0), "afternoon": time(14, 0), "evening": time(18, 0),
         "night": TONIGHT_TIME}
PART_ENDS = {"morning": time(12, 0), "afternoon": time(17, 0), "evening": time(21, 0),
             "night": time(23, 59)}


def _split_day_time(rest: str) -> tuple[bool, time | None]:
    """rest is what follows a day word. Returns (ok, time_or_None)."""
    rest = rest.strip()
    if not rest:
        return True, None
    if rest in PARTS:
        return True, PARTS[rest]
    t = _parse_time(rest)
    return (t is not None), t


def _this_part(part: str, now: datetime) -> datetime | None:
    """"this afternoon" = today 14:00; if that has passed but it is still afternoon, one hour
    from now; once the afternoon is over, None (the caller asks when)."""
    start = _at(now.date(), PARTS[part])
    if start > now:
        return start
    if now.time() < PART_ENDS[part]:
        return now + timedelta(hours=1)
    return None


def parse_when(phrase: str, now: datetime) -> datetime | None:
    """Parse one due phrase. Returns an ET-aware datetime or None if not understood."""
    now = now.astimezone(ET)
    today = now.date()
    p = re.sub(r"\s+", " ", phrase.strip().lower()).rstrip(".!")
    p = _LEAD.sub("", p)
    if not p:
        return None

    if p == "tonight":
        return _at(today, TONIGHT_TIME)
    for part in PARTS:
        if p in (part, "this " + part):
            return _this_part(part, now)

    m = _REL.match(p)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        if unit.startswith("m"):
            return now + timedelta(minutes=n)
        if unit.startswith("h"):
            return now + timedelta(hours=n)
        if unit.startswith("d"):
            return _at(today + timedelta(days=n), DEFAULT_TIME)
        return _at(today + timedelta(weeks=n), DEFAULT_TIME)

    for word, offset in (("today", 0), ("tomorrow", 1), ("tmrw", 1)):
        if p == word or p.startswith(word + " "):
            ok, t = _split_day_time(p[len(word):])
            if ok:
                return _at(today + timedelta(days=offset), t or DEFAULT_TIME)
            return None

    words = p.split(" ", 2)
    is_next = words[0] == "next"
    if words[0] in ("next", "this"):
        words = words[1:]
    if words and words[0] in _WEEKDAYS:
        ok, t = _split_day_time(" ".join(words[1:]))
        if not ok:
            return None
        target = _WEEKDAYS[words[0]]
        if is_next:
            # Max's rule: "next Wednesday" = the Wednesday of the FOLLOWING calendar week.
            # Weeks start on WEEK_START (US calendar: Sunday). On Sun 10/11 next Wed = 10/21;
            # on Mon 10/5, Thu 10/8 and Sat 10/10 next Wed = 10/14.
            week_start = today - timedelta(days=(today.weekday() - WEEK_START) % 7)
            return _at(week_start + timedelta(days=7 + (target - WEEK_START) % 7), t or DEFAULT_TIME)
        # plain / "this" <weekday> = the nearest upcoming one (today if its time is still ahead)
        ahead = (target - today.weekday()) % 7
        dt = _at(today + timedelta(days=ahead), t or DEFAULT_TIME)
        if ahead == 0 and dt <= now:
            dt = _at(today + timedelta(days=7), t or DEFAULT_TIME)
        return dt
    if is_next:
        return None                  # "next <something that is not a weekday>"

    first, _, rest = p.partition(" ")
    m = _DATE.match(first)
    if m:
        ok, t = _split_day_time(rest)
        if not ok:
            return None
        mo, dy, yr = int(m.group(1)), int(m.group(2)), m.group(3)
        year = today.year if yr is None else (int(yr) + 2000 if len(yr) == 2 else int(yr))
        try:
            d = date(year, mo, dy)
        except ValueError:
            return None
        if yr is None and d < today - timedelta(days=1):
            d = date(year + 1, mo, dy)   # "1/15" said in October means next January
        return _at(d, t or DEFAULT_TIME)

    t = _parse_time(p)
    if t is not None:
        dt = _at(today, t)
        return dt if dt > now else _at(today + timedelta(days=1), t)
    return None


_PREFIX = re.compile(r"^\s*(add|park|idea|did)\s*:\s*", re.IGNORECASE)


def capture_kind(text: str) -> str | None:
    """'add', 'park', 'idea' or 'did' if the message starts with that word and a colon."""
    m = _PREFIX.match(text)
    return m.group(1).lower() if m else None


def parse_add(text: str, now: datetime) -> tuple[str, datetime | None]:
    """Split '#inbox' text into (title, due). Tries the longest trailing phrase first."""
    body = _PREFIX.sub("", text, count=1).strip()
    words = body.split()
    for k in range(min(5, len(words) - 1), 0, -1):   # always leave at least one title word
        due = parse_when(" ".join(words[-k:]), now)
        if due is not None:
            title = " ".join(words[:-k])
            title = re.sub(r"\s+(?:by|on|due|at|before)$", "", title, flags=re.IGNORECASE)
            return title.strip(" ,;-"), due
    return body, None
