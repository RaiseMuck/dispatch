"""The once-a-minute loop body. tick() decides what (if anything) to post right now.

It knows nothing about Discord: it hands Message objects to a Poster.
PrintPoster prints them (dry-run); DiscordPoster queues them for the bot to send.

Per day, outside quiet hours (00:00-08:00 ET):
  - morning post: first tick from 08:00 to 11:59 -> the 3 active items + anything past due
  - catch-up: if the PC was off all morning, the first tick after noon sends the same list
  - hourly batched nag: every due, unanswered item in ONE message, at most once an hour
  - weekly review: Sundays from 08:00 (or any day if the last one was 7+ days ago) ->
    every waiting, queued and parked title grouped by area, so nothing stays invisible
Phase 2.1: the morning post carries the three day-mode buttons and a Swap button per active
item; the first mode-free morning after a mode day re-picks untouched mode fillers; a deadline
that can't fit today's mode is flagged "can't fit today - pick a day?" in every hourly message;
a waiting item's check-in rides the hourly message (once).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Protocol

from . import __version__, rules
from .areas import AREAS, UNSORTED
from .clock import ET, Clock, fmt_et
from .db import Task
from .service import TaskService, describe

META_MORNING = "last_morning_date"
META_BATCH = "last_batch_at"
META_WEEKLY = "last_weekly_date"
META_LIST_DONE = "list_done_event_id"   # newest event already covered by a list-after-done
WEEKLY_DAY = 6            # Monday=0 ... Sunday=6


@dataclass
class Message:
    kind: str                      # "morning", "catchup", "nag", "weekly", "list" or "note"
    header: str
    items: list[Task]              # items that get buttons
    mention: bool = True
    footer: str = ""
    listed: list[Task] = field(default_factory=list)   # shown as text only (weekly review)
    body: list[str] = field(default_factory=list)
    why: dict[int, str] = field(default_factory=dict)   # task id -> rank explanation (dry-run)
    undo: list[tuple[int, int]] = field(default_factory=list)   # (event id, task id) -> Undo buttons
    flags: dict[int, str] = field(default_factory=dict)  # task id -> "can't fit today (...)"
    wait_more: dict[int, int] = field(default_factory=dict)   # waiting id -> extensions so far
    mode_row: bool = False                 # morning/catch-up: Sick/home day, Slammed, Normal
    split_offer: int | None = None         # task id the bot offers to split (follow-up note)
    buttons: list[tuple[str, str]] = field(default_factory=list)   # extra (label, custom_id)

    def buttons_for(self, t: Task) -> list[str]:
        if t.id in self.flags:
            return rules.flagged_options()
        if t.status == "waiting":
            return rules.waiting_options(self.wait_more.get(t.id, 0))
        return rules.snooze_options(t.snooze_count)

    def swap_ids(self) -> list[int]:
        """One Swap button per ACTIVE item (queued extras, flagged and waiting items get none)."""
        if self.kind not in ("morning", "catchup", "nag", "list"):
            return []
        return [t.id for t in self.items if t.status == "active" and t.id not in self.flags]

    def shown(self) -> list[Task]:
        return self.items + self.listed

    def render(self, mention_text: str = "@Max") -> str:
        lines = [(mention_text + " " if self.mention else "") + self.header]
        for i, t in enumerate(self.items, 1):
            flag = f" <- {self.flags[t.id]}" if t.id in self.flags else ""
            lines.append(f"  {i}. {describe(t)}{flag}")
        lines.extend(self.body)
        if self.footer:
            lines.append(self.footer)
        return "\n".join(lines)


class Poster(Protocol):
    def send(self, msg: Message) -> None: ...


class PrintPoster:
    """Dry-run poster: prints what would go to #today, including each item's buttons."""

    def __init__(self, clock: Clock, out=print):
        self.clock = clock
        self.out = out
        self.sent: list[tuple[datetime, Message]] = []

    def send(self, msg: Message) -> None:
        now = self.clock.now()
        self.sent.append((now, msg))
        self.out(f"[{fmt_et(now)} ET] #today ({msg.kind})")
        self.out("  " + msg.render("<@MAX_USER_ID>").replace("\n", "\n  "))
        for t in msg.items:
            btns = " ".join(f"[{rules.LABELS[b]}]" for b in msg.buttons_for(t))
            self.out(f"      buttons #{t.id}: {btns}")
            if t.id in msg.why:
                self.out(f"      why #{t.id}: {msg.why[t.id]}")
        if msg.swap_ids():
            self.out("      swap row: " + " ".join(f"[Swap #{i}]" for i in msg.swap_ids()))
        if msg.mode_row:
            self.out("      mode row: " + " ".join(f"[{rules.MODE_LABELS[m]}]"
                                                   for m in rules.MODE_NAMES))
        if msg.buttons:
            self.out("      buttons: " + " ".join(f"[{label}]" for label, _ in msg.buttons))
        if msg.split_offer is not None:
            self.out(f"      (the bot follows up with a split offer for #{msg.split_offer} "
                     "if the local AI proposes a first step)")
        if msg.undo:
            self.out("      buttons: " + " ".join(f"[Undo #{tid}]" for _, tid in msg.undo))


class DiscordPoster:
    """Collects messages during a tick; the bot's async loop sends them afterwards."""

    def __init__(self) -> None:
        self.outbox: list[Message] = []

    def send(self, msg: Message) -> None:
        self.outbox.append(msg)

    def drain(self) -> list[Message]:
        out, self.outbox = self.outbox, []
        return out


@dataclass
class RunState:
    heartbeat_path: Path | None = None
    bot_connected: bool = False
    extra: dict = field(default_factory=dict)


def write_heartbeat(path: Path, now: datetime, bot_connected: bool,
                    features: dict | None = None) -> None:
    data = {"last_loop_at": now.isoformat(), "bot_connected": bot_connected,
            "version": __version__, "features": features or {"canvas": False, "siri": False}}
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _send(poster: Poster, sent: list, msg: Message) -> None:
    poster.send(msg)
    sent.append(msg)


def new_from_canvas(service: TaskService, now: datetime) -> list[Task]:
    """Canvas items created in the last 24 h (named in the morning footer so none is invisible)."""
    since = now - timedelta(hours=24)
    return [t for t in service.by_status("active", "queued", "parked")
            if t.source == "canvas" and t.created_at >= since]


def moved_by_canvas(service: TaskService, now: datetime) -> list[tuple[int, datetime]]:
    """Canvas date moves in the last 24 h that were not undone: (task id, new due)."""
    from .db import detail_of, from_db, to_db
    rows = service.conn.execute(
        "SELECT task_id, detail FROM events WHERE kind='rescheduled' AND actor='canvas' "
        "AND undone_by IS NULL AND at >= ? ORDER BY id", (to_db(now - timedelta(hours=24)),)).fetchall()
    out = {}
    for r in rows:
        new = detail_of(r).get("new_due")
        if new:
            out[r["task_id"]] = datetime.fromisoformat(str(new))
    return list(out.items())


def flags_for(service: TaskService, tasks: list[Task], now: datetime) -> dict[int, str]:
    """Deadlines within 48 h that today's day mode keeps out of the three."""
    mode = service.current_mode(now)
    if mode is None:
        return {}
    return {t.id: f"can't fit today ({rules.why_not(t, mode)}) - pick a day?"
            for t in tasks if t.status == "queued" and rules.urgent(t, now)
            and not rules.fits(t, mode)}


def _checkin_msg_bits(service: TaskService, checkins: list[Task]) -> dict[int, int]:
    return {t.id: service.wait_more_count(t.id) for t in checkins}


def _daily_list(service: TaskService, poster: Poster, now: datetime, sent: list, late: bool) -> None:
    """Morning post (or the after-noon catch-up when the PC was off all morning).

    The three are the active items in rank order (rules.rank); each one's rank, score and the
    reason are stored in its morning_post event so stats can measure the daily-three hit rate."""
    pairs = service.morning_repick(now)
    service.promote_queued(actor="scheduler")
    active = service.morning_items(now)
    due = service.due_batch(now)
    checkins = service.due_checkins(now)
    extra = [t for t in due if t.status != "active"] + checkins
    n = len(active)
    if late:
        header = f"Missed the 8 AM post (PC was off?). Your {n} for today, plus anything waiting:"
    else:
        header = f"Good morning - {now:%a} {now.month}/{now.day}. Your {n} for today:"
    past = [t for t in extra if t.status != "waiting"]
    footer = f"Also past deadline (queued): {', '.join('#%d' % t.id for t in past)}" if past else ""
    if checkins:
        line = "Check-in day (still waiting on someone?): " + \
            ", ".join(f"#{t.id}" for t in checkins)
        footer = f"{footer}\n{line}" if footer else line
    if pairs:
        mode = rules.MODE_LABELS.get(
            service.real_mode_on(now.astimezone(ET).date() - timedelta(days=1)),
            "day mode").lower()
        line = (f"Back after yesterday's {mode}: {', '.join('#%d' % i.id for _, i in pairs)} "
                f"({', '.join('#%d' % o.id for o, _ in pairs)} "
                f"{'returns' if len(pairs) == 1 else 'return'} to the queue). "
                "Say \"undo that\" to keep yesterday's three.")
        footer = f"{footer}\n{line}" if footer else line
    offer = service.split_candidate(now)
    if offer is not None:
        line = f"#{offer.id} has been sitting a while - I'll suggest a small first step below."
        footer = f"{footer}\n{line}" if footer else line
    fresh = new_from_canvas(service, now)
    if fresh:
        line = f"New from Canvas since yesterday: {', '.join('#%d' % t.id for t in fresh)}"
        footer = f"{footer}\n{line}" if footer else line
    moved = moved_by_canvas(service, now)
    if moved:
        line = "Canvas moved since yesterday: " + ", ".join(f"#{tid} -> {fmt_et(d)}" for tid, d in moved)
        footer = f"{footer}\n{line}" if footer else line
    why = {t.id: rules.explain(t, now) for t in active + extra if t.status != "waiting"}
    _send(poster, sent, Message("catchup" if late else "morning", header, active + extra,
                                footer=footer, why=why, flags=flags_for(service, extra, now),
                                wait_more=_checkin_msg_bits(service, checkins), mode_row=True,
                                split_offer=offer.id if offer else None))
    for i, t in enumerate(active, 1):
        service.log(t.id, "morning_post", actor="scheduler", rank=i,
                    score=round(rules.score(t, now), 4), why=why[t.id], late=late,
                    snoozes=t.snooze_count)
    for t in extra:
        if t.status != "waiting":
            service.log(t.id, "morning_post", actor="scheduler", extra=True,
                        snoozes=t.snooze_count)
    if due:
        service.record_nags(due)
    if checkins:
        service.record_nags(checkins, kind="checkin_sent")
    service.meta_set(META_MORNING, now.date().isoformat())
    service.meta_set(META_BATCH, now.isoformat())


def list_after_done(service: TaskService, now: datetime) -> Message | None:
    """Max finished something (button, chat, phone call or CLI): the list as it stands now -
    his three in rank order, then anything else due now (the same list the phone line reads).

    Finds the 'done' events since the last call (a meta high-water mark, so each is covered
    once, across restarts); several in a row give ONE list. Skipped: 'did:' items (they were
    never on the list), anything undone since, and quiet hours (the 8 AM post covers those).
    The first run ever only sets the mark. Quiet: no @-mention, no nag recorded, the hourly
    timer untouched."""
    from .db import detail_of
    top = service.conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]
    last = service.meta_get(META_LIST_DONE)
    service.meta_set(META_LIST_DONE, str(top))
    if last is None or not last.isdigit():
        return None
    rows = service.conn.execute(
        "SELECT id, task_id, detail FROM events WHERE kind='done' AND id > ? AND id <= ? "
        "AND undone_by IS NULL ORDER BY id", (int(last), top)).fetchall()
    rows = [r for r in rows if detail_of(r).get("was") != "done"]       # 'did:' logs
    if not rows or rules.in_quiet_hours(now):
        return None
    done = [service.get(r["task_id"]) for r in rows]
    marks = ",".join("?" * len(rows))
    up = [r["task_id"] for r in service.conn.execute(
        f"SELECT task_id FROM events WHERE kind='promoted' AND undone_by IS NULL "
        f"AND cause_id IN ({marks}) ORDER BY id", [r["id"] for r in rows]).fetchall()]
    active, extra = service.today_list(now)
    names = ", ".join(f"#{t.id} {t.title}" for t in done)
    header = f"Done: {names}. " + ("Your list now:" if active or extra
                                   else "Nothing else in your three right now.")
    lines = []
    moved = [t for t in active if t.id in up]
    if moved:
        lines.append("Moved up: " + ", ".join(f"#{t.id} {t.title}" for t in moved) + ".")
    if extra:
        lines.append("Also due now (outside your three): "
                     + ", ".join(f"#{t.id}" for t in extra))
    why = {t.id: rules.explain(t, now) for t in active + extra}
    return Message("list", header, active + extra, mention=False, footer="\n".join(lines),
                   why=why, flags=flags_for(service, extra, now))


def weekly_due(last: str | None, now: datetime) -> bool:
    """Sunday from 08:00; or any day if the last review was 7+ days ago (PC off on Sunday)."""
    if now.time() < rules.MORNING_AT:
        return False
    today = now.date()
    if last == today.isoformat():
        return False
    if now.weekday() == WEEKLY_DAY:
        return True
    return last is not None and (today - date.fromisoformat(last)).days >= 7


def _weekly(service: TaskService, poster: Poster, now: datetime, sent: list) -> None:
    waiting = service.by_status("queued", "parked", "waiting")
    body: list[str] = []
    blocked = [t for t in waiting if t.status == "waiting"]
    if blocked:
        body.append("**Waiting**")
        for t in sorted(blocked, key=lambda t: (t.waiting_until or now, t.id)):
            when = f" - check-in {fmt_et(t.waiting_until)}" if t.waiting_until else ""
            body.append(f"  - #{t.id} {t.title}{when}")
    for area in list(AREAS) + [UNSORTED]:
        group = [t for t in waiting if t.area == area and t.status != "waiting"]
        if not group:
            continue
        body.append(f"**{area}**")
        for t in rules.rank(group, now):
            due = f" (due {fmt_et(t.due_at)})" if t.due_at else ""
            park = " [parked]" if t.status == "parked" else ""
            body.append(f"  - #{t.id} {t.title}{due}{park}")
    never = service.never_fit(now)
    if never:
        body.append("Didn't fit any day this week: " + ", ".join(f"#{t.id}" for t in never)
                    + " - split it, pick a day, or kill it?")
    if waiting:
        header = (f"Weekly review - {len(waiting)} waiting behind your active 3 "
                  "(queued, parked or waiting on someone):")
    else:
        header = "Weekly review - nothing is waiting in the queue or parked."
    _send(poster, sent, Message("weekly", header, [], listed=waiting, body=body))
    service.record_nags(waiting, kind="weekly_review")
    service.meta_set(META_WEEKLY, now.date().isoformat())


def tick(service: TaskService, poster: Poster, clock: Clock, state: RunState) -> list[Message]:
    now = clock.now()
    sent: list[Message] = []
    if not rules.in_quiet_hours(now):
        today = now.date().isoformat()
        if service.meta_get(META_MORNING) != today:
            # Before noon: the normal morning post. After noon (PC was off): same list as a catch-up.
            _daily_list(service, poster, now, sent, late=not rules.in_morning_window(now))
        else:
            raw = service.meta_get(META_BATCH)
            last_batch = datetime.fromisoformat(raw) if raw else None
            due = service.due_batch(now)
            checkins = service.due_checkins(now)
            if (due or checkins) and now >= rules.next_batch_time(last_batch, now):
                items = due + checkins
                if due:
                    word = "item" if len(items) == 1 else "items"
                    header = f"{len(items)} {word} waiting on you ({fmt_et(now)}):"
                else:
                    header = ("Still waiting on someone? Check-in day for "
                              + ", ".join(f"#{t.id} {t.title}" for t in checkins) + ".")
                _send(poster, sent, Message("nag", header, items,
                                            flags=flags_for(service, due, now),
                                            wait_more=_checkin_msg_bits(service, checkins)))
                if due:
                    service.record_nags(due)
                if checkins:
                    service.record_nags(checkins, kind="checkin_sent")
                service.meta_set(META_BATCH, now.isoformat())
        if weekly_due(service.meta_get(META_WEEKLY), now):
            _weekly(service, poster, now, sent)
    if state.heartbeat_path is not None:
        write_heartbeat(state.heartbeat_path, now, state.bot_connected, state.extra.get("features"))
    return sent
