"""Phone-call holds and callbacks (added 2026-10-18).

"No calls until 5pm" / "no calls today" / "I'm busy for 2 hours" stop Dispatch from phoning
Max; "call me at 3" / "call me back in 20 minutes" asks it to. Both work from Discord (typed,
matched by rule - never by the AI) and from a phone call (the phone AI's hold_calls tool).
Everything lives in the meta table; nothing here talks to Vapi.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, time, timedelta

from .clock import ET, fmt_et
from .parser import parse_when

HOLD_KEY, CALLBACK_KEY = "calls_hold_until", "calls_callback"

_NO_CALLS = re.compile(
    r"\s*(?:no|stop|don'?t|do not|pause)\s+(?:more\s+)?call(?:s|ing)?(?:\s+me)?"
    r"(?:\s+(?P<rest>.+?))?\s*[.!]*\s*", re.IGNORECASE)
_BUSY = re.compile(r"\s*(?:i'?m|i am)\s+busy\s+(?P<rest>(?:for|until|till|til)\s+.+?)\s*[.!]*\s*",
                   re.IGNORECASE)
_RESUME = re.compile(r"\s*(?:calls? on|resume calls|you can call(?: me)?(?: again)?|calls are ok)"
                     r"\s*[.!]*\s*", re.IGNORECASE)
_CALL_ME = re.compile(r"\s*call me(?: back)?\s+(?P<rest>(?:at|in|around|tonight|tomorrow).*?)"
                      r"\s*[.!]*\s*", re.IGNORECASE)
_FOR = re.compile(r"for\s+(?:an?\s+)?(\d+|an?|half an?)?\s*(min(?:ute)?s?|hours?|hrs?|h)\b", re.I)


def end_of_day(now: datetime) -> datetime:
    d = now.astimezone(ET).date() + timedelta(days=1)
    return datetime.combine(d, time(0, 0), tzinfo=ET)


def parse_until(rest: str | None, now: datetime) -> datetime | None:
    """'today' / '' -> midnight; 'for 2 hours' / 'for 20 minutes' / 'for an hour'; 'until 5pm'."""
    rest = (rest or "").strip().lower().rstrip(".!")
    if rest in ("", "today", "for today", "rest of the day", "for the rest of the day", "tonight"):
        return end_of_day(now)
    m = _FOR.search(rest)
    if m:
        n_raw, unit = (m.group(1) or "1"), m.group(2).lower()
        n = 0.5 if n_raw.startswith("half") else (1 if n_raw in ("a", "an") else int(n_raw))
        delta = timedelta(minutes=n) if unit.startswith("m") else timedelta(hours=n)
        return now + delta
    rest = re.sub(r"^(until|till|til|to|at)\s+", "", rest)
    return parse_when(rest, now)


def hold_until(service, now: datetime) -> datetime | None:
    v = service.meta_get(HOLD_KEY)
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    return dt if dt > now else None


def set_hold(service, until: datetime | None) -> None:
    service.meta_set(HOLD_KEY, until.isoformat() if until else None)


def callback(service) -> dict | None:
    v = service.meta_get(CALLBACK_KEY)
    return json.loads(v) if v else None


def set_callback(service, at: datetime | None, reason: str = "") -> None:
    service.meta_set(CALLBACK_KEY, json.dumps({"at": at.isoformat(), "reason": reason[:200]})
                     if at else None)


def command(service, text: str, now: datetime) -> str | None:
    """A Discord message about calls -> the reply, or None if it isn't one."""
    t = text or ""
    m = _NO_CALLS.fullmatch(t) or _BUSY.fullmatch(t)
    if m:
        until = parse_until(m.group("rest"), now)
        if until is None or until <= now:
            return ("When should calls start again? e.g. \"no calls until 5pm\", \"no calls for "
                    "2 hours\" or \"no calls today\".")
        set_hold(service, until)
        cb = callback(service)
        extra = ""
        if cb and datetime.fromisoformat(cb["at"]) < until:
            set_callback(service, None)
            extra = " Your earlier call-me-back is cancelled too."
        return (f"No calls until {fmt_et(until)}. Reminders here keep coming as usual.{extra} "
                "Say \"calls on\" to undo.")
    if _RESUME.fullmatch(t):
        set_hold(service, None)
        return "Calls are back on."
    m = _CALL_ME.fullmatch(t)
    if m:
        rest = m.group("rest")
        at = parse_until(rest if not rest.lower().startswith("in ") else "for " + rest[3:], now)
        if at is None or at <= now:
            return "When? e.g. \"call me at 3pm\" or \"call me in 20 minutes\"."
        set_callback(service, at, "you asked me to call you back")
        hold = hold_until(service, now)
        if hold and hold > at:
            set_hold(service, at)
        return f"I'll call you at {fmt_et(at)}."
    return None


def status(service, now: datetime) -> str:
    bits = []
    h = hold_until(service, now)
    if h:
        bits.append(f"no calls until {fmt_et(h)}")
    cb = callback(service)
    if cb:
        bits.append(f"callback at {fmt_et(datetime.fromisoformat(cb['at']))}")
    return "; ".join(bits) or "calls allowed"
