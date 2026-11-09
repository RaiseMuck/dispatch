"""Canvas (OSU CarmenCanvas) calendar feed -> School tasks.

Canvas publishes a private calendar link (an .ics file - a standard calendar text format) under
Calendar -> Calendar Feed. The bot reads it every 2 hours. Each assignment/event becomes a task
with source 'canvas', area School, external_ref 'canvas:<UID>' (the event's permanent id), and
the due date from the feed. Re-reads move the due date of an OPEN item if the professor moved it
(a normal 'rescheduled' event, so it can be undone), never bring back a done/killed item, and
skip anything already past. A broken or unreachable feed never crashes anything: it is logged
(without the link - the link is a secret) and Max gets at most one short note per day.

Standard library only: a small reader for the few calendar fields Canvas uses.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import qwen, rules
from .clock import ET, UTC, fmt_et
from .db import Task
from .service import NotOpen, TaskService

log = logging.getLogger("dispatch.canvas")

CANVAS_POLL_EVERY = timedelta(hours=2)
META_POLL = "canvas_last_poll_at"
META_ERR_NOTED = "canvas_error_noted_date"
META_ERR_PENDING = "canvas_error_pending"
FETCH_TIMEOUT = 20
ALL_DAY_DUE = time(23, 59)          # an all-day Canvas item is "due that day"


@dataclass
class CanvasEvent:
    uid: str
    summary: str
    due: datetime | None
    description: str = ""
    url: str = ""

    @property
    def ref(self) -> str:
        return "canvas:" + self.uid

    def notes(self) -> str:
        head = f"Canvas: {self.url}" if self.url else "Canvas"
        desc = self.description.strip()[:200]
        return f"{head}\n{desc}" if desc else head


@dataclass
class SyncResult:
    added: list[Task] = field(default_factory=list)
    moved: list[Task] = field(default_factory=list)
    skipped_past: int = 0
    skipped_closed: int = 0
    unchanged: int = 0
    error: str | None = None
    move_events: list[dict] = field(default_factory=list)   # {task_id, event_id, old, new}

    def summary(self) -> str:
        if self.error:
            return f"canvas feed error: {self.error}"
        return (f"canvas: added {len(self.added)}, moved {len(self.moved)}, unchanged "
                f"{self.unchanged}, skipped past {self.skipped_past}, skipped closed {self.skipped_closed}")


# ---- reading the .ics text -----------------------------------------------------------------
def unfold(text: str) -> list[str]:
    """Calendar files wrap long lines: a line starting with a space/tab continues the previous."""
    out: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and out:
            out[-1] += raw[1:]
        elif raw:
            out.append(raw)
    return out


def unescape(s: str) -> str:
    return re.sub(r"\\([\\,;nN])", lambda m: "\n" if m.group(1) in "nN" else m.group(1), s)


_warned_zones: set[str] = set()


def event_due(params: list[str], value: str) -> datetime | None:
    """DTSTART -> an ET datetime. All-day (VALUE=DATE) -> 23:59 ET that day."""
    p = {}
    for x in params:
        if "=" in x:
            k, v = x.split("=", 1)
            p[k.upper()] = v.strip('"')
    value = value.strip()
    try:
        if p.get("VALUE", "").upper() == "DATE" or re.fullmatch(r"\d{8}", value):
            d = date(int(value[:4]), int(value[4:6]), int(value[6:8]))
            return datetime.combine(d, ALL_DAY_DUE, tzinfo=ET)
        fmt = "%Y%m%dT%H%M%S" if len(value.rstrip("Z")) == 15 else "%Y%m%dT%H%M"
        naive = datetime.strptime(value.rstrip("Z"), fmt)
    except ValueError:
        return None
    if value.endswith("Z"):
        return naive.replace(tzinfo=UTC).astimezone(ET)
    zone = ET
    if "TZID" in p:
        try:
            zone = ZoneInfo(p["TZID"])
        except (ZoneInfoNotFoundError, ValueError):
            if p["TZID"] not in _warned_zones:
                _warned_zones.add(p["TZID"])
                log.warning("unknown time zone %r in the Canvas feed; treating as Eastern", p["TZID"])
    return naive.replace(tzinfo=zone).astimezone(ET)


def parse_ics(text: str) -> list[CanvasEvent]:
    """Events with a UID and a DTSTART. Raises ValueError if this is not a calendar at all
    (e.g. an expired link that returns a login web page)."""
    lines = unfold(text or "")
    if not any(ln.strip().upper() == "BEGIN:VCALENDAR" for ln in lines):
        raise ValueError("not a calendar feed")
    found: dict[str, CanvasEvent] = {}
    cur: dict | None = None
    for ln in lines:
        up = ln.strip().upper()
        if up == "BEGIN:VEVENT":
            cur = {}
        elif up == "END:VEVENT":
            if cur is not None and "UID" in cur and "DTSTART" in cur:
                uid = cur["UID"][1].strip()
                found[uid] = CanvasEvent(
                    uid=uid, summary=unescape(cur.get("SUMMARY", ([], "(untitled)"))[1]).strip(),
                    due=event_due(*cur["DTSTART"]),
                    description=unescape(cur.get("DESCRIPTION", ([], ""))[1]),
                    url=cur.get("URL", ([], ""))[1].strip())
            cur = None
        elif cur is not None and ":" in ln:
            head, value = ln.split(":", 1)
            name, *params = head.split(";")
            cur[name.strip().upper()] = (params, value)
    return list(found.values())


def fetch(url: str, timeout: float = FETCH_TIMEOUT) -> str:
    """Download the feed. Raises on any network/HTTP problem (the caller logs the type only)."""
    req = urllib.request.Request(url, headers={"Accept": "text/calendar, */*",
                                               "User-Agent": "Dispatch/0.2"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


# ---- applying the feed ---------------------------------------------------------------------
def poll_due(service: TaskService, now: datetime) -> bool:
    raw = service.meta_get(META_POLL)
    if not raw:
        return True
    try:
        return now - datetime.fromisoformat(raw) >= CANVAS_POLL_EVERY
    except ValueError:
        return True


def new_events(service: TaskService, events: list[CanvasEvent], now: datetime) -> list[CanvasEvent]:
    """Future events whose ref is not in the database yet (these need a field guess)."""
    return [e for e in events if e.due is not None and e.due >= now
            and service.get_by_ref(e.ref) is None]


def guess_for(ev: CanvasEvent, base_url: str = qwen.DEFAULT_URL) -> qwen.Guess:
    return qwen.guess_fields(ev.summary, ev.notes(), base_url)


def apply(service: TaskService, events: list[CanvasEvent], now: datetime,
          guess=None) -> SyncResult:
    """guess(event) -> qwen.Guess (payoff/likelihood/estimate; the area is always School)."""
    res = SyncResult()
    for ev in events:
        if ev.due is None:
            continue
        t = service.get_by_ref(ev.ref)
        if t is None:
            if ev.due < now:
                res.skipped_past += 1          # never add history
                continue
            try:
                g = guess(ev) if guess else qwen.Guess()
            except Exception:  # noqa: BLE001 - a guess can never block an add
                g = qwen.Guess()
            if not isinstance(g, qwen.Guess):
                g = qwen.Guess()
            guessed = [f for f in g.guessed if f != "area"]
            info = g.info()
            info["guess"].pop("area", None)
            task, created = service.add_unique(
                title=ev.summary[:300] or "(untitled Canvas item)", area="School", due_at=ev.due,
                source="canvas", notes=ev.notes(), external_ref=ev.ref,
                payoff=g.payoff if "payoff" in guessed else None,
                likelihood=g.likelihood if "likelihood" in guessed else None,
                estimate_minutes=g.estimate_minutes if "estimate_minutes" in guessed else None,
                guessed=guessed, actor="canvas", info={**info, "via": "canvas", "feed_due": ev.due},
                fill_by_rank=True, **qwen.needs_kwargs(g))
            remember_feed_due(service, ev.ref, ev.due)
            if created:
                res.added.append(service.get(task.id))
            else:
                res.unchanged += 1
            continue
        # Compare the feed with what the FEED said last time - never with the task's current
        # date, which Max may have changed himself (his own move must survive every re-poll).
        last = last_feed_due(service, t)
        if last is None and not _max_moved_it(service, t.id):
            last = t.due_at            # no feed record at all, and Max never moved it: the
            #                            task's date IS the last feed date (review r2 should-fix 2)
        if last is None:
            remember_feed_due(service, ev.ref, ev.due)     # Max moved it; start tracking now
            res.unchanged += 1
        elif abs((last - ev.due).total_seconds()) < 60:
            remember_feed_due(service, ev.ref, ev.due)
            res.unchanged += 1                # title changes are ignored; the UID is the identity
        elif t.status in ("done", "killed"):
            remember_feed_due(service, ev.ref, ev.due)
            res.skipped_closed += 1           # never resurrect
        else:
            try:
                before = t.due_at
                moved, _ = service.reschedule(t.id, ev.due, actor="canvas")
                res.moved.append(moved)
                res.move_events.append({"task_id": t.id, "event_id": service.last_event_id,
                                        "old": before.isoformat() if before else None,
                                        "new": ev.due.isoformat()})
            except NotOpen:
                res.skipped_closed += 1
            remember_feed_due(service, ev.ref, ev.due)
    if res.move_events:
        queue_move_notes(service, res.move_events)
    return res


# ---- what the feed said last time, per item (so Max's own moves are never overwritten) --------
def _feed_key(ref: str) -> str:
    return "canvas_feed_due:" + ref


def _max_moved_it(service: TaskService, task_id: int) -> bool:
    """True if anyone other than the Canvas sync ever rescheduled this task."""
    return service.conn.execute(
        "SELECT 1 FROM events WHERE task_id=? AND kind='rescheduled' AND COALESCE(actor,'') != 'canvas' "
        "LIMIT 1", (task_id,)).fetchone() is not None


def remember_feed_due(service: TaskService, ref: str, due: datetime) -> None:
    service.meta_set(_feed_key(ref), due.isoformat())


def last_feed_due(service: TaskService, t: Task) -> datetime | None:
    raw = service.meta_get(_feed_key(t.external_ref))
    if raw:
        return datetime.fromisoformat(raw)
    # Older rows: the date the feed gave when the item was created (stored in its event).
    row = service.conn.execute("SELECT detail FROM events WHERE task_id=? AND kind='created'",
                               (t.id,)).fetchone()
    if row and row[0]:
        d = json.loads(row[0])
        val = d.get("feed_due") or (d.get("due_at") if d.get("via") == "canvas" else None)
        if val:
            return datetime.fromisoformat(val).astimezone(ET)
    return None


# ---- telling Max about date moves (one line + Undo; batched, never in quiet hours) -----------
META_MOVES_PENDING = "canvas_moves_pending"


def queue_move_notes(service: TaskService, moves: list[dict]) -> None:
    pending = service.meta_json(META_MOVES_PENDING) or {"moves": []}
    pending["moves"].extend(moves)
    service.meta_set(META_MOVES_PENDING, json.dumps(pending))


def flush_move_notes(service: TaskService, poster, now: datetime) -> bool:
    """Send queued Canvas date moves as ONE quiet note with an Undo button per move."""
    pending = service.meta_json(META_MOVES_PENDING)
    if not pending or not pending.get("moves") or poster is None or rules.in_quiet_hours(now):
        return False
    from .scheduler import Message
    lines, undo = [], []
    for m in pending["moves"]:
        t = service.get(m["task_id"])
        if t is None:
            continue
        old = fmt_et(datetime.fromisoformat(m["old"])) if m.get("old") else "no date"
        lines.append(f"#{t.id} {t.title[:70]}: now due {fmt_et(datetime.fromisoformat(m['new']))} "
                     f"(was {old})")
        undo.append((m["event_id"], t.id))
    service.meta_set(META_MOVES_PENDING, None)
    if not lines:
        return False
    if len(lines) == 1:
        header = f"Canvas moved a date - {lines[0]}. Tap Undo if that's wrong."
        body = []
    else:
        header = f"Canvas moved {len(lines)} dates (tap Undo on any that are wrong):"
        body = [f"  - {ln}" for ln in lines]
    poster.send(Message("canvas_move", header, items=[], mention=False, body=body, undo=undo))
    return True


# ---- failures: logged, and at most one Discord note per day, never in quiet hours ------------
def record_failure(service: TaskService, error: str, now: datetime) -> None:
    log.warning("canvas feed didn't load: %s", error)       # never the link itself
    if service.meta_get(META_ERR_NOTED) != now.date().isoformat():
        service.meta_set(META_ERR_PENDING, error)


def flush_note(service: TaskService, poster, now: datetime) -> bool:
    """Send the pending failure note if allowed now. Returns True if a note was sent."""
    error = service.meta_get(META_ERR_PENDING)
    if not error or poster is None or rules.in_quiet_hours(now):
        return False
    service.meta_set(META_ERR_PENDING, None)
    today = now.date().isoformat()
    if service.meta_get(META_ERR_NOTED) == today:
        return False
    from .scheduler import Message                       # late import: scheduler imports service
    poster.send(Message("note", f"Canvas feed didn't load today ({error}). I'll keep trying every "
                        "2 hours; if this repeats, get a fresh link: Canvas -> Calendar -> Calendar "
                        "Feed, and paste it into .env as CANVAS_ICS_URL.", items=[], mention=False))
    service.meta_set(META_ERR_NOTED, today)
    return True


def sync(service: TaskService, now: datetime, fetcher, guess=None, force: bool = False) -> SyncResult | None:
    """poll_due + fetch + parse + apply, all in this thread (tests, dry-run). None = not due yet."""
    if not force and not poll_due(service, now):
        return None
    service.meta_set(META_POLL, now.isoformat())
    try:
        events = parse_ics(fetcher())
    except Exception as exc:  # noqa: BLE001 - a dead feed must never crash the bot
        record_failure(service, type(exc).__name__, now)
        return SyncResult(error=type(exc).__name__)
    service.meta_set(META_ERR_PENDING, None)       # it works again: no stale note
    return apply(service, events, now, guess)


async def poll_in_bot(service: TaskService, url: str, now: datetime,
                      base_url: str = qwen.DEFAULT_URL, run=asyncio.to_thread) -> SyncResult | None:
    """The bot's version: the download and the AI guesses run in worker threads (they can take
    seconds and must not stall Discord); database reads/writes stay on the event-loop thread."""
    if not url or not poll_due(service, now):
        return None
    service.meta_set(META_POLL, now.isoformat())
    try:
        events = parse_ics(await run(fetch, url))
    except Exception as exc:  # noqa: BLE001
        record_failure(service, type(exc).__name__, now)
        return SyncResult(error=type(exc).__name__)
    service.meta_set(META_ERR_PENDING, None)
    fresh = new_events(service, events, now)

    def guess_all() -> dict:
        return {e.uid: guess_for(e, base_url) for e in fresh}

    try:
        guesses = await run(guess_all)
    except Exception:  # noqa: BLE001
        guesses = {}
    res = apply(service, events, now, guess=lambda e: guesses.get(e.uid, qwen.Guess()))
    log.info(res.summary())
    return res
