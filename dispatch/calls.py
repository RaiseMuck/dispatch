"""Dispatch calls Max (added 2026-10-18). Off until VAPI_OUTBOUND_NUMBER_ID is set (the free
Vapi number can only receive calls).

Why it calls, in priority order:
  1. a callback he asked for ("call me at 3", from Discord or a call) - rings on time;
  2. an errand finished - he hears the result (errands.py sets a callback for it);
  3. accountability, with a Discord heads-up 5 minutes before so he can say "no calls":
     a. something on today's list is overdue or due within 24 h and hasn't been touched for
        3 hours (a deadline still ahead first - see call_subject);
     b. it's 2 PM or later, nothing is done today, and he hasn't touched the list today.
Guard rails: accountability calls only 10 AM-9 PM ET, callbacks 8 AM-11 PM; at most
MAX_PER_DAY calls a day and one per item per day; any hold ("no calls until 5") wins; and no
call once this month's Vapi spend reaches the budget (default $13 of a $15 test month).
Unanswered (2026-10-30): once a check-in call goes unanswered,
no more check-in calls that day; callbacks he asked for still ring.
Class and work hours (BUSY_BLOCKS, added 2026-10-28): no calls of any kind inside them. A callback
that comes due inside one is pushed to when the block ends; an accountability call is skipped.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, time, timedelta, timezone

from . import db, holds, rules
from .clock import ET, fmt_et
from .voice import (BRIEF_KEY, UNANSWERED_KEY, assistant_payload, build_prompt, cut, snapshot,
                    speakable)

log = logging.getLogger("dispatch.calls")

PENDING_KEY, LOG_KEY, SPEND_KEY = "call_pending", "calls_made:", "voice_spend"
WARN = timedelta(minutes=5)
STALL = timedelta(hours=3)
GAP = timedelta(hours=2)          # minimum time between two accountability calls
MAX_PER_DAY = 3
ACCOUNT_HOURS = (time(10, 0), time(21, 0))
CALLBACK_HOURS = (time(8, 0), time(23, 0))
DEAD_AFTERNOON = time(14, 0)
MAX_ACTORS = ("button", "chat", "siri")

# Fixed class and work hours, ET, Monday=0: no accountability calls inside them (or in the
# BUSY_PAD before). Back-to-back classes can be one block so the walks between stay quiet.
# Example: {1: [("09:00", "12:30", "class")], 4: [("10:00", "16:00", "work")]}
BUSY_PAD = timedelta(minutes=10)   # also quiet for 10 minutes before each block starts
BUSY_BLOCKS: dict[int, list[tuple[str, str, str]]] = {}


def busy_until(now: datetime) -> tuple[datetime, str] | None:
    """(end of block, what it is) if `now` falls in class/work hours (or the pad before), else None."""
    d = now.astimezone(ET)
    for s, e, what in BUSY_BLOCKS.get(d.weekday(), ()):
        start = datetime.combine(d.date(), time.fromisoformat(s), tzinfo=ET) - BUSY_PAD
        end = datetime.combine(d.date(), time.fromisoformat(e), tzinfo=ET)
        if start <= d < end:
            return end, what
    return None


def _in(now: datetime, span) -> bool:
    return span[0] <= now.astimezone(ET).time() < span[1]


def _day_log(service, now: datetime) -> dict:
    v = service.meta_get(LOG_KEY + now.astimezone(ET).date().isoformat())
    return json.loads(v) if v else {"count": 0, "items": [], "dead": False, "budget_note": False}


def _save_day(service, now: datetime, d: dict) -> None:
    service.meta_set(LOG_KEY + now.astimezone(ET).date().isoformat(), json.dumps(d))


def month_spend(service, vapi, now: datetime, run_sync=None) -> float:
    """This month's Vapi spend in dollars, cached for an hour (GET /call, sum of `cost`)."""
    c = json.loads(service.meta_get(SPEND_KEY) or "{}")
    month = now.astimezone(ET).strftime("%Y-%m")
    if c.get("month") == month and c.get("at") and \
            now - datetime.fromisoformat(c["at"]) < timedelta(hours=1):
        return float(c.get("usd", 0.0))
    start = now.astimezone(ET).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    # Vapi only keeps 14 days of call history on this plan (older requests are rejected), so ask for
    # at most the last 13 days and keep a running per-call ledger for the rest of the month.
    since = max(start, now - timedelta(days=13)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    calls = vapi.call("GET", f"/call?createdAtGt={since}&limit=1000") or []
    led = json.loads(service.meta_get("voice_spend_ledger") or "{}")
    if led.get("month") != month:
        led = {"month": month, "calls": {}}
    for i, x in enumerate(calls):
        led["calls"][x.get("id") or f"anon{i}"] = float(x.get("cost") or 0)
    service.meta_set("voice_spend_ledger", json.dumps(led))
    usd = round(sum(led["calls"].values()), 2)
    service.meta_set(SPEND_KEY, json.dumps({"month": month, "at": now.isoformat(), "usd": usd}))
    return usd


def _touched_since(service, task_id: int, since: datetime) -> bool:
    acts = ",".join("?" * len(MAX_ACTORS))
    return service.conn.execute(
        f"SELECT 1 FROM events WHERE task_id=? AND at >= ? AND actor IN ({acts}) LIMIT 1",
        (task_id, db.to_db(since), *MAX_ACTORS)).fetchone() is not None


def _any_max_action_today(service, now: datetime) -> bool:
    start = datetime.combine(now.astimezone(ET).date(), time(0), tzinfo=ET)
    acts = ",".join("?" * len(MAX_ACTORS))
    return service.conn.execute(
        f"SELECT 1 FROM events WHERE at >= ? AND actor IN ({acts}) AND kind != 'chat' LIMIT 1",
        (db.to_db(start), *MAX_ACTORS)).fetchone() is not None


def _done_today(service, now: datetime) -> int:
    start = datetime.combine(now.astimezone(ET).date(), time(0), tzinfo=ET)
    return service.conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind='done' AND undone_by IS NULL AND at >= ?",
        (db.to_db(start),)).fetchone()[0]


def _called_before(service, task_id: int, now: datetime, days: int = 2) -> int:
    """On how many of the last `days` days a check-in call was about this item."""
    d = now.astimezone(ET).date()
    return sum(task_id in (service.meta_json(LOG_KEY + (d - timedelta(days=i)).isoformat()) or {})
               .get("items", []) for i in range(1, days + 1))


def call_subject(service, now: datetime, day: dict):
    """The item a check-in call is about. 2026-11-02: it used to walk only the three picks, so it
    rang about the same stale #119/#109/#95 three days running while a homework deadline that
    night went unmentioned. Now: any open item due within 24 h and untouched for 3 h; a deadline
    due tonight beats one due tomorrow, which beats one already missed (each best-ranked first); an item already called
    about on each of the last two days rests until he touches it."""
    pool = [t for t in service.by_status("active", "queued")
            if t.due_at is not None and t.id not in day["items"]
            and t.due_at - now <= timedelta(hours=24)
            and _called_before(service, t.id, now) < 2
            and not _touched_since(service, t.id, now - STALL)]
    midnight = datetime.combine(now.astimezone(ET).date() + timedelta(days=1), time(0), tzinfo=ET)
    tonight = rules.rank([t for t in pool if now <= t.due_at < midnight], now)
    later = rules.rank([t for t in pool if t.due_at >= midnight], now)
    return (tonight + later + rules.rank([t for t in pool if t.due_at < now], now) or [None])[0]


def accountability_reason(service, now: datetime, day: dict) -> tuple[str, int | None] | None:
    """(reason, task id or None) for an accountability call, or None."""
    t = call_subject(service, now, day)
    if t is not None:
        return (f"{t.title} is {_when(t.due_at, now)}.", t.id)
    d = now.astimezone(ET)
    if (not day["dead"] and d.time() >= DEAD_AFTERNOON and _done_today(service, now) == 0
            and service.mode_record(now) is None and not _any_max_action_today(service, now)):
        return ("it's after 2 and nothing on your list has moved today.", None)
    return None


# ---- what the call says ---------------------------------------------------------------------
# 2026-11-02, from the call logs: every voicemail was "Number N, <title cut at 60>, is overdue and
# hasn't moved in 3 hours" - which told him nothing, since something is always overdue. The
# briefing names the one thing, why it matters now, and the rest of the day in counts.
VOICEMAIL_WORDS = 70              # ~28 s at his voice speed
# Abbreviations in his titles, said in full (test call 11-02: "BF homework" came out "by the f").
SAY_AS = {"BF": "Behavioral Finance", "Prof.": "Professor"}


def _clock(dt: datetime) -> str:
    return f"{dt.astimezone(ET):%I:%M %p}".lstrip("0").replace(":00", "")


def _when(due: datetime, now: datetime) -> str:
    d, n = due.astimezone(ET), now.astimezone(ET)
    if due < now:
        days = (n.date() - d.date()).days
        return "overdue" if days < 1 else ("overdue since yesterday" if days == 1
                                           else f"{days} days overdue")
    if d.date() == n.date():
        return f"due at {_clock(due)} " + ("tonight" if d.hour >= 18 else "today")
    if d.date() == n.date() + timedelta(days=1):
        return f"due tomorrow at {_clock(due)}"
    return f"due {d:%A}"


_FILLER = {"a", "an", "the", "to", "of", "for", "and", "or", "with", "on", "at", "in", "by", "from"}


def _spoken_title(title: str) -> str:
    """The head of the title, at most 8 words: stops at an aside ('(', ' - ', ' + ') and never
    ends on a dangling 'to the'."""
    t = re.split(r"\s+[-+(]\s*|\s*\(", title, maxsplit=1)[0].strip() or title
    words = t.split()[:8]
    while len(words) > 2 and words[-1].lower().strip(",;:") in _FILLER:
        words.pop()
    return " ".join(words).rstrip(" ,;:")


def _why(t) -> str:
    """The first sentence of its notes, if it reads as a reason (short, no ticket refs)."""
    if not t.notes:
        return ""
    first = re.split(r"(?<=[.!?])\s", " ".join(t.notes.split()).replace("->", "so"), maxsplit=1)[0]
    ok = 4 <= len(first.split()) <= 20 and not re.search(r"[#/]|\.md\b|https?:", first)
    return first.rstrip(".") + "." if ok else ""


def briefing(service, now: datetime, top=None, lead: str = "") -> dict:
    """What a call says: {"opener": first words if he answers, "voicemail": the message left at the
    beep, "discord": the same in text}. `top` is the item the call is about (None = the most
    pressing one); `lead` is said first (a callback's reason, "nothing has moved today")."""
    midnight = datetime.combine(now.astimezone(ET).date() + timedelta(days=1), time(0), tzinfo=ET)
    dated = [t for t in service.by_status("active", "queued") if t.due_at is not None]
    today = sorted((t for t in dated if now <= t.due_at < midnight), key=lambda t: t.due_at)
    late = rules.rank([t for t in dated if t.due_at < now], now)
    soon = sorted((t for t in dated if midnight <= t.due_at < midnight + timedelta(days=2)),
                  key=lambda t: t.due_at)
    if top is None:
        top = (today + late or [None])[0]
    skip = {top.id} if top is not None else set()
    today, late, soon = ([t for t in xs if t.id not in skip] for xs in (today, late, soon))

    def spoken(why: bool, n_soon: int) -> list[str]:
        parts = [lead] if lead else []
        if top is not None:
            parts.append(f"First: {_spoken_title(top.title)}, {_when(top.due_at, now)}."
                         + (f" {_why(top)}" if why and _why(top) else ""))
        if today:
            parts.append(f"{len(today)} more {'thing is' if len(today) == 1 else 'things are'} "
                         "due today.")
        if soon[:n_soon]:
            parts.append("Coming up: " + "; and ".join(
                f"{_spoken_title(t.title)}, {_when(t.due_at, now).removeprefix('due ')}"
                for t in soon[:n_soon]) + ".")
        if late:
            parts.append(f"{len(late)} overdue.")
        return parts

    for why, n_soon in ((True, 2), (False, 2), (False, 1), (False, 0)):
        vm = ["Max, it's Augustine."] + spoken(why, n_soon) + ["The full list is in Discord."]
        if len(" ".join(vm).split()) <= VOICEMAIL_WORDS:
            break
    head = spoken(False, 0)[:2 if lead else 1]
    plus = f" Plus {len(today)} more due today." if top is not None and today else ""
    opener = " ".join(["Hey Max, it's Augustine."] + head) + plus + " What's the plan?"

    lines = [lead] if lead else []
    if top is not None:
        lines.append(f"**#{top.id} {top.title}** - {_when(top.due_at, now)}"
                     + (f" - {_why(top)}" if _why(top) else ""))
    if today:
        lines.append("Also due today: " + "; ".join(f"#{t.id} {cut(t.title, 60)} ({_clock(t.due_at)})"
                                                     for t in today))
    if soon:
        lines.append("Coming up: " + "; ".join(f"#{t.id} {cut(t.title, 60)} ({fmt_et(t.due_at)})"
                                               for t in soon[:4]))
    if late:
        lines.append(f"Overdue: {len(late)} (" + ", ".join(f"#{t.id}" for t in late[:10])
                     + (", ..." if len(late) > 10 else "") + ")")
    def say(s: str) -> str:                          # "Homework #1" -> "Homework number 1"
        s = re.sub(r"(?<![\w&])#(\d+)\b", r"number \1", s)
        for short, full in SAY_AS.items():
            s = re.sub(rf"\b{re.escape(short)}(?!\w)", full, s)
        return speakable(s)
    return {"opener": say(opener), "voicemail": say(" ".join(vm)), "discord": "\n".join(lines)}


def call_payload(link, phone_number_id: str, brief: dict, now: datetime) -> dict:
    prompt = build_prompt(snapshot(link.s, now), link.pin, now, outbound=brief["discord"])
    model = assistant_payload(prompt)["model"]
    return {"assistantId": link.assistant_id, "phoneNumberId": phone_number_id,
            "customer": {"number": link.max_phone},
            "assistantOverrides": {
                "firstMessage": brief["opener"], "model": model,
                # A machine answers: wait for the beep, leave the briefing, hang up - no AI turn,
                # no 30 s of recorded silence (2026-11-02: 21 of 40 calls went to voicemail).
                "voicemailDetection": {"provider": "vapi", "type": "audio",
                                       "backoffPlan": {"startAtSeconds": 2, "frequencySeconds": 2.5,
                                                       "maxRetries": 6},
                                       "beepMaxAwaitSeconds": 25},
                "voicemailMessage": brief["voicemail"]}}


def tick(service, link, phone_number_id: str, now: datetime, budget: float) -> list[str]:
    """One minute of the call engine. Places at most one call. Returns Discord messages.
    Network calls happen here (run it in a worker thread from the bot)."""
    msgs: list[str] = []
    if not phone_number_id or link is None:
        return msgs
    day = _day_log(service, now)
    hold = holds.hold_until(service, now)
    pending = json.loads(service.meta_get(PENDING_KEY) or "null")

    def over_budget() -> bool:
        try:
            spent = month_spend(service, link.vapi, now)
        except Exception:  # noqa: BLE001 - can't check the spend: don't call
            log.exception("spend check failed")
            return True
        if spent >= budget:
            if not day["budget_note"]:
                day["budget_note"] = True
                _save_day(service, now, day)
                msgs.append(f"\U0001F4DE Call budget reached (${spent:.2f} of ${budget:.2f} this "
                            "month), so I've stopped calling. Discord reminders keep going.")
            return True
        return False

    def place(brief: dict, task_id: int | None, kind: str) -> None:
        if over_budget():
            return
        link.vapi.call("POST", "/call", call_payload(link, phone_number_id, brief, now))
        service.meta_set(BRIEF_KEY, json.dumps({"at": now.isoformat(), "text": brief["discord"]}))
        day["count"] += 1
        if task_id:
            day["items"].append(task_id)
        if kind in ("dead", "stall"):
            day["dead"] = True                       # one "nothing moved" nudge a day is plenty
            day["last"] = now.isoformat()
        _save_day(service, now, day)
        log.info("outbound call placed (%s)", kind)

    # 1-2. a callback he asked for (or an errand result) - no heads-up needed
    cb = holds.callback(service)
    if cb and datetime.fromisoformat(cb["at"]) <= now:
        busy = busy_until(now)
        if busy and not hold:
            end, what = busy
            holds.set_callback(service, end, cb["reason"])
            msgs.append(f"\U0001F4DE You're in {what}, so I'll call at {fmt_et(end)} instead.")
            return msgs
        holds.set_callback(service, None)
        if hold or not _in(now, CALLBACK_HOURS) or day["count"] >= MAX_PER_DAY:
            msgs.append(f"\U0001F4DE I was going to call you ({cb['reason']}) but calls are paused "
                        "or it's outside call hours - here it is instead.")
            return msgs
        lead = cb["reason"][:1].upper() + cb["reason"][1:].rstrip(".") + "."
        place(briefing(service, now, lead=lead), None, "callback")
        return msgs

    # he let an earlier check-in call ring out today: stop calling about it (2026-10-30 - the
    # logs showed 3 unanswered calls a day for days on end, each talking into a full voicemail)
    if service.meta_get(UNANSWERED_KEY + now.astimezone(ET).date().isoformat()):
        if pending:
            service.meta_set(PENDING_KEY, None)
        return msgs

    # 3. a pending accountability call whose heads-up has run out
    if pending:
        if datetime.fromisoformat(pending["at"]) > now:
            return msgs
        service.meta_set(PENDING_KEY, None)
        if hold or busy_until(now) or not _in(now, ACCOUNT_HOURS) or day["count"] >= MAX_PER_DAY:
            return msgs
        still = accountability_reason(service, now, day)
        if still is None:
            return msgs                                   # he got moving during the heads-up
        top = service.get(still[1]) if still[1] else None
        lead = "" if top else "It's after 2 and nothing on your list has moved today."
        place(briefing(service, now, top, lead), still[1], "stall" if still[1] else "dead")
        return msgs

    if hold or not _in(now, ACCOUNT_HOURS) or day["count"] >= MAX_PER_DAY:
        return msgs
    if busy_until(now) or busy_until(now + WARN):     # no heads-up for a call that would land in class
        return msgs
    if day.get("last") and now - datetime.fromisoformat(day["last"]) < GAP:
        return msgs
    found = accountability_reason(service, now, day)
    if found is None:
        return msgs
    reason, task_id = found
    at = now + WARN
    service.meta_set(PENDING_KEY, json.dumps({"at": at.isoformat(), "reason": reason,
                                              "task": task_id, "kind": "dead" if task_id is None else "stall"}))
    msgs.append(f"\U0001F4DE Heads up: calling you at {fmt_et(at)} - {reason} Reply "
                "\"no calls for an hour\" or \"no calls today\" to stop it, or just get moving.")
    return msgs
