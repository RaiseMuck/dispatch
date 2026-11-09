"""TaskService: every change to a task goes through here, and every change writes an event row.

Phase 2: every mutating event stores detail["before"] (the columns it changed, as stored) so
undo() can put them back, and side effects point at their cause via events.cause_id (e.g. the
'promoted' row caused by a 'done'). `actor` says who did it: button, chat, siri, cli, canvas,
scheduler, seed (NULL on Phase 1 rows).
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

from . import db, rules
from .areas import UNSORTED, normalize
from .clock import ET, Clock, fmt_et
from .db import Task
from .parser import capture_kind, parse_add

OPEN = ("active", "queued", "parked", "waiting")
UNDO_WINDOW = timedelta(hours=48)
UNDOABLE = ("created", "done", "killed", "snoozed", "do_now", "rescheduled", "estimated",
            "prioritized", "area_set", "shifted",
            "mode_set", "repick", "swapped", "waiting", "resumed", "split", "needs_set")
# Rows that record something without changing a task: they never block an undo.
NOT_A_CHANGE = ("nag_sent", "morning_post", "weekly_review", "chat", "undone", "checkin_sent")
MAX_ACTORS = ("button", "chat", "siri")          # what "undo that" may reverse
FIELD_KIND = {"estimate_minutes": "estimated", "payoff": "prioritized",
              "likelihood": "prioritized", "area": "area_set",
              "place": "needs_set", "energy": "needs_set"}
WHAT = {"created": "add", "done": "done", "killed": "kill", "snoozed": "snooze",
        "do_now": "do-now", "rescheduled": "move", "estimated": "estimate change",
        "prioritized": "priority change", "area_set": "area change", "shifted": "shift",
        "mode_set": "day mode", "repick": "re-pick", "swapped": "swap", "waiting": "wait",
        "resumed": "resume", "split": "split", "needs_set": "place/energy change"}
PAST = {"created": "added", "done": "marked done", "killed": "killed", "snoozed": "snoozed",
        "do_now": "started with Do now", "rescheduled": "moved", "estimated": "re-estimated",
        "prioritized": "re-prioritized", "area_set": "moved to another area",
        "shifted": "shifted", "promoted": "promoted into the active three",
        "mode_set": "set aside by a day mode", "repick": "re-picked", "swapped": "swapped out",
        "waiting": "set to waiting", "resumed": "taken off waiting", "split": "split",
        "needs_set": "given a new place/energy"}
STARTED_PIN = timedelta(hours=1)                # "new list" never bumps what you just started
MODE_KEY = "day_mode:"                           # + ET date -> JSON (see set_mode)


def mode_key(now: datetime) -> str:
    return MODE_KEY + now.astimezone(ET).date().isoformat()


class NotOpen(Exception):
    """The task is already done/killed (e.g. an old button was tapped)."""


class LadderError(Exception):
    """A snooze was asked for after the two allowed snoozes were used."""


class UndoRefused(Exception):
    """Undo is not possible; str(exc) is a plain-language reason for Max."""


class CantDo(Exception):
    """A request that makes no sense in the item's state; str(exc) is a reason for Max."""


class NotActive(CantDo):
    """Only one of the three (an active item) can be swapped; a waiting item can't be snoozed."""


@dataclass
class UndoResult:
    text: str
    event_id: int                       # the 'undone' row
    reversed_ids: list[int] = field(default_factory=list)
    promoted: list[Task] = field(default_factory=list)


@dataclass
class ModeResult:
    text: str
    event_id: int | None                # the 'mode_set' row (None = nothing changed)
    demoted: list[Task] = field(default_factory=list)
    promoted: list[Task] = field(default_factory=list)
    active: list[Task] = field(default_factory=list)
    same: bool = False


@dataclass
class SwapResult:
    task: Task
    replacement: Task | None = None
    event_id: int | None = None         # the 'swapped' row (None = nothing changed)
    forced: bool = False                # 3rd attempt: no change, Max must decide
    nothing_fits: bool = False
    long_swaps: int = 0                 # 'Too long' swaps of this item so far (split offer at 2)


@dataclass
class StartResult:
    task: Task
    event_id: int
    demoted: Task | None = None


def _queued_nag(t: Task, now: datetime) -> datetime | None:
    """When a demoted item should next nag: undated -> never (it waits in the queue), due within
    48 h -> now (it keeps showing hourly, flagged), else on its due date."""
    if t.due_at is None:
        return None
    if rules.urgent(t, now):
        return min(t.due_at, now)
    return t.due_at


def _mins(t: Task) -> str:
    return rules.fmt_minutes(rules.needs(t)[2])


def _snap(t: Task, cols) -> dict:
    """The given columns of t exactly as stored in the database (for event 'before')."""
    out = {}
    for c in cols:
        v = getattr(t, c)
        if isinstance(v, datetime):
            v = db.to_db(v)
        elif c == "guessed":
            v = json.dumps(v) if v else None
        out[c] = v
    return out


class TaskService:
    def __init__(self, conn: sqlite3.Connection, clock: Clock, cap: int = rules.ACTIVE_CAP):
        self.conn = conn
        self.clock = clock
        self.cap = cap
        self.last_event_id: int | None = None   # id of the event the last mutation wrote

    # ---- reads ---------------------------------------------------------------------------
    def get(self, task_id: int) -> Task | None:
        return db.get_task(self.conn, task_id)

    def get_by_ref(self, ref: str) -> Task | None:
        return db.get_task_by_ref(self.conn, ref)

    def by_status(self, *statuses: str) -> list[Task]:
        marks = ",".join("?" * len(statuses))
        return db.tasks_where(self.conn, f"status IN ({marks})", statuses)

    def open_tasks(self) -> list[Task]:
        return self.by_status(*OPEN)

    def active_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM tasks WHERE status='active'").fetchone()[0]

    def morning_items(self, now: datetime | None = None) -> list[Task]:
        return rules.rank(self.by_status("active"), now or self.clock.now())

    def due_batch(self, now: datetime | None = None) -> list[Task]:
        return rules.batch_due(self.by_status("active", "queued"), now or self.clock.now())

    def today_list(self, now: datetime | None = None) -> tuple[list[Task], list[Task]]:
        """The list Discord and the phone line both show: the three in rank order, then every
        other item due for a reminder now (urgent or overdue), ranked."""
        now = now or self.clock.now()
        active = self.morning_items(now)
        ids = {t.id for t in active}
        return active, rules.rank([t for t in self.due_batch(now) if t.id not in ids], now)

    def _open(self, task_id: int) -> Task:
        t = self.get(task_id)
        if t is None or t.status not in OPEN:
            raise NotOpen(f"task {task_id} is not open")
        return t

    @staticmethod
    def _not_waiting(t: Task) -> None:
        """A waiting item never nags, so a snooze/Do now on it would silently do nothing."""
        if t.status == "waiting":
            raise NotActive(f"#{t.id} is waiting on someone - say \"got it on #{t.id}\" when "
                            "they get back to you, or pick a day.")

    def _set(self, task_id: int, **cols) -> None:
        cols = {k: (db.to_db(v) if isinstance(v, datetime) else v) for k, v in cols.items()}
        sets = ", ".join(f"{k} = ?" for k in cols)
        self.conn.execute(f"UPDATE tasks SET {sets} WHERE id = ?", (*cols.values(), task_id))

    def log(self, task_id: int | None, kind: str, actor: str | None = None, **detail) -> int:
        """Record a non-mutating event (chat message, morning post line). Returns its id."""
        with self.conn:
            return db.log_event(self.conn, task_id, kind, self.clock.now(), actor=actor, **detail)

    # ---- mutations -----------------------------------------------------------------------
    def add(self, title: str, area: str | None = None, due_at: datetime | None = None,
            source: str = "inbox", notes: str | None = None,
            estimate_minutes: int | None = None, status: str | None = None,
            payoff: float | None = None, likelihood: float | None = None,
            guessed: list[str] | None = None, external_ref: str | None = None,
            actor: str | None = None, info: dict | None = None,
            fill_by_rank: bool = False, place: str | None = None,
            energy: str | None = None) -> Task:
        """status=None -> the active-cap rule decides (active if a slot is free, else queued).
        Only an explicit status='parked' (park:/idea: prefix, or a seed row) parks a task;
        the area - even one guessed by Qwen - never parks anything by itself.
        fill_by_rank=True (Canvas / CLI adds, which can arrive in bursts): the item is filed
        as queued and a free slot is then filled by rank, not by arrival order.
        A duplicate external_ref raises sqlite3.IntegrityError (see add_unique)."""
        now = self.clock.now()
        area = normalize(area)
        place = place if place in rules.PLACES else None
        energy = energy if energy in rules.ENERGIES else None
        if status not in (None, "active", "queued", "parked"):
            raise ValueError(f"bad status {status!r}")
        promote_after = fill_by_rank and status is None
        if promote_after:
            status = "queued"
        elif status is None or (status == "active" and self.active_count() >= self.cap):
            status = rules.apply_active_cap(self.active_count(), self.cap)
        if status == "active":
            next_nag = due_at or now        # an undated active item is "due now"
        elif status == "queued":
            # queued items nag once their deadline arrives - except one already due within 48 h,
            # which shows in the hourly message from now on (same rule as a demoted item).
            # Fixed 2026-10-21: a quiz closing at 11:59 PM, added that morning behind three
            # non-urgent active items, was set to ping first at 11:59 PM.
            next_nag = (min(due_at, now) if due_at is not None
                        and due_at - now <= rules.URGENT_WINDOW else due_at)
        else:
            next_nag = None                 # parked ideas never nag
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO tasks(title, area, status, due_at, estimate_minutes, payoff, "
                "likelihood, snooze_count, next_nag_at, created_at, source, notes, external_ref, "
                "guessed, place, energy) VALUES (?,?,?,?,?,?,?,0,?,?,?,?,?,?,?,?)",
                (title, area, status, db.to_db(due_at), estimate_minutes, payoff, likelihood,
                 db.to_db(next_nag), db.to_db(now), source, notes, external_ref,
                 json.dumps(guessed) if guessed else None, place, energy))
            tid = cur.lastrowid
            self.last_event_id = db.log_event(self.conn, tid, "created", now, actor=actor,
                                              status=status, area=area, due_at=due_at,
                                              source=source, **(info or {}))
        if promote_after:
            created_id = self.last_event_id
            # best-ranked queued item takes a free slot; linked to the add so Undo works on it
            self.promote_queued(cause_id=created_id, actor=actor)
            self.last_event_id = created_id
        return self.get(tid)

    def add_unique(self, **kw) -> tuple[Task, bool]:
        """add() with dedupe on external_ref: a second add with the same ref returns the
        existing task (whatever its status - a done item is never resurrected) and False."""
        ref = kw.get("external_ref")
        if ref:
            existing = self.get_by_ref(ref)
            if existing is not None:
                return existing, False
        try:
            return self.add(**kw), True
        except sqlite3.IntegrityError:
            existing = self.get_by_ref(ref) if ref else None
            if existing is None:
                raise
            return existing, False

    def capture(self, text: str, area_picker: Callable[[str], str]) -> Task:
        """'#inbox' path. 'add:' -> active/queued by the cap rule; 'park:'/'idea:' -> parked.

        The picker (Qwen) only chooses the area; any failure falls back to unsorted."""
        kind = capture_kind(text) or "add"
        title, due = parse_add(text, self.clock.now())
        try:
            area = area_picker(title)
        except Exception:  # noqa: BLE001 - the picker must never block capture
            area = UNSORTED
        if kind == "did":
            return self.log_done(title, area=area)
        status = "parked" if kind in ("park", "idea") else None
        return self.add(title, area=area, due_at=due, source="inbox", status=status)

    def promote_queued(self, exclude: int | None = None, cause_id: int | None = None,
                       actor: str | None = None, only: list[int] | None = None,
                       via: str | None = None) -> list[Task]:
        """Fill free active slots from the queue, best first. Returns the promoted tasks.
        Each 'promoted' row records the status/next_nag it replaced and the event that caused it.

        Phase 2.1: only items that fit today's day mode are candidates (light first on a sick
        day), never one swapped out today, and a mode's total-minutes budget stops the fill.
        `only` limits the candidates (a swap's chosen replacement)."""
        now = self.clock.now()
        mode = self.current_mode(now)
        away = self.swapped_out_today(now)
        promoted = []
        with self.conn:
            while self.active_count() < self.cap:
                queued = [t for t in self.by_status("queued") if t.id != exclude
                          and t.id not in away and (only is None or t.id in only)]
                ordered = rules.fit_rank(queued, mode, now)
                if not ordered:
                    break
                nxt = ordered[0]
                if mode is not None and mode.total_minutes:
                    used = sum(rules.needs(t)[2] for t in self.by_status("active"))
                    if used + rules.needs(nxt)[2] > mode.total_minutes:
                        break
                nag = nxt.next_nag_at or nxt.due_at or now
                self._set(nxt.id, status="active", next_nag_at=nag)
                extra = {"via": via} if via else {}
                if mode is not None:
                    extra["mode"] = mode.name
                db.log_event(self.conn, nxt.id, "promoted", now, actor=actor, cause_id=cause_id,
                             before=_snap(nxt, ("status", "next_nag_at")),
                             score=round(rules.score(nxt, now), 4), **extra)
                promoted.append(self.get(nxt.id))
        return promoted

    # ---- day mode (Phase 2.1) ------------------------------------------------------------
    def mode_record(self, now: datetime | None = None) -> dict | None:
        """Today's stored mode row (see set_mode), or None. Expires at midnight ET by key."""
        return self.meta_json(mode_key(now or self.clock.now()))

    def current_mode(self, now: datetime | None = None) -> rules.DayMode | None:
        """Today's day mode; None when none is set or it is Normal (= no filter)."""
        rec = self.mode_record(now)
        if not rec or rec.get("mode") == "normal":
            return None
        try:
            return rules.DayMode.from_json(rec.get("constraints") or {})
        except (TypeError, ValueError):
            return None

    def swapped_out_today(self, now: datetime | None = None) -> set[int]:
        now = now or self.clock.now()
        start = datetime.combine(now.astimezone(ET).date(), datetime.min.time(), tzinfo=ET)
        rows = self.conn.execute("SELECT task_id FROM events WHERE kind='swapped' AND "
                                 "undone_by IS NULL AND at >= ?", (db.to_db(start),)).fetchall()
        return {r[0] for r in rows}

    def set_mode(self, mode: rules.DayMode, actor: str | None = None,
                 source: str = "chat") -> ModeResult:
        """Tell Dispatch what kind of day it is. Active items that don't fit go back to the queue
        (no snooze used), fitting queued items fill the three by rank, and the mode is stored
        for today only. One 'mode_set' event holds everything Undo needs."""
        now = self.clock.now()
        key = mode_key(now)
        old = self.meta_get(key)
        old_rec = self.meta_json(key)
        label = rules.MODE_LABELS.get(mode.name, "Today")
        if old_rec and old_rec.get("constraints") == mode.to_json() or \
                (mode.name == "normal" and self.current_mode(now) is None):
            text = ("Already a normal day - nothing is filtered." if mode.name == "normal"
                    else f"Already set: {label.lower()} ({mode.describe()}).")
            return ModeResult(text, None, active=self.morning_items(now), same=True)
        demote = [t for t in self.by_status("active") if not rules.fits(t, mode)]
        # Leaving a mode (e.g. "back to normal" the same afternoon): untouched items the old
        # mode put in the three give their slots back to better items that now fit - the same
        # re-pick the next morning would do, in this same transaction (review r1 fix 2).
        pairs = []
        if old_rec and old_rec.get("mode") not in (None, "normal"):
            pairs = self._filler_pairs(None if mode.name == "normal" else mode, now,
                                       skip={t.id for t in demote})
        moved = demote + [t for pair in pairs for t in pair]
        with self.conn:
            for t in demote:
                self._set(t.id, status="queued", next_nag_at=_queued_nag(t, now))
            for out, into in pairs:
                self._set(out.id, status="queued", next_nag_at=_queued_nag(out, now))
                self._set(into.id, status="active",
                          next_nag_at=into.next_nag_at or into.due_at or now)
            eid = db.log_event(self.conn, None, "mode_set", now, actor=actor, source=source,
                               date=key[len(MODE_KEY):], mode=mode.name,
                               constraints=mode.to_json(), before={"meta": old},
                               demoted=[t.id for t in demote],
                               repicked=[[o.id, i.id] for o, i in pairs],
                               side=[{"id": t.id, "before": _snap(t, ("status", "next_nag_at"))}
                                     for t in moved])
            db.meta_set(self.conn, key, json.dumps(
                {"mode": mode.name, "constraints": mode.to_json(), "set_at": db.to_db(now),
                 "event_id": eid, "source": source}))
        promoted = self.promote_queued(cause_id=eid, actor=actor, via="mode")
        self.last_event_id = eid
        active = self.morning_items(now)
        demoted = [self.get(t.id) for t in demote]
        if mode.name == "normal":
            lines = ["Normal day: no filter."]
        else:
            name = {"sick": "Sick/home day", "slammed": "Slammed day"}.get(mode.name, "Today")
            lines = [f"{name}: {mode.describe()}."]
        if pairs:
            lines.append("Back in your three: " + ", ".join(f"#{i.id}" for _, i in pairs)
                         + " (" + ", ".join(f"#{o.id}" for o, _ in pairs)
                         + f" {'goes' if len(pairs) == 1 else 'go'} back to the queue).")
        if active:
            lines.append("Your three now: " + ", ".join(
                f"#{t.id} {t.title} (~{_mins(t)})" for t in active) + ".")
        if demoted:
            bits = []
            for t in demoted:
                why = rules.why_not(t, mode)
                if t.due_at is not None and rules.urgent(t, now):
                    why += f" - due {fmt_et(t.due_at)}, it stays in the hourly message"
                bits.append(f"#{t.id} {t.title} ({why})")
            lines.append("Set aside for today: " + ", ".join(bits) + ".")
        if not active:
            kind = {"sick": "a sick day", "slammed": "a slammed day"}.get(mode.name, "today")
            lines.append(f"Nothing on the list fits {kind} - your three are cleared for today. "
                         "Deadlines still show in the hourly message; say \"I've got 30 minutes\" "
                         "later or add something small.")
        return ModeResult(" ".join(lines), eid, demoted, promoted, active)

    def mode_fillers(self) -> list[Task]:
        """Active items a day mode (or a swap) put in the three and Max has not touched since."""
        out = []
        acts = ",".join("?" * len(MAX_ACTORS))
        for t in self.by_status("active"):
            p = self.conn.execute(
                "SELECT * FROM events WHERE task_id=? AND kind='promoted' AND undone_by IS NULL "
                "ORDER BY id DESC LIMIT 1", (t.id,)).fetchone()
            if p is None:
                continue
            d = db.detail_of(p)
            if d.get("via") not in ("mode", "swap") and d.get("mode") in (None, "normal"):
                continue
            touched = self.conn.execute(
                f"SELECT 1 FROM events WHERE task_id=? AND id>? AND actor IN ({acts}) LIMIT 1",
                (t.id, p["id"], *MAX_ACTORS)).fetchone()
            if touched is None:
                out.append(t)
        return out

    def _filler_pairs(self, mode: rules.DayMode | None, now: datetime,
                      skip: set[int] = frozenset()) -> list[tuple[Task, Task]]:
        """(untouched mode filler, better queued item that fits `mode`) pairs, worst filler
        first. Items swapped out today never come back through this."""
        fillers = sorted((f for f in self.mode_fillers() if f.id not in skip),
                         key=lambda t: rules.fit_key(t, mode, now), reverse=True)
        away = self.swapped_out_today(now)
        queued = rules.fit_rank([q for q in self.by_status("queued") if q.id not in away],
                                mode, now)
        pairs = []
        for f in fillers:
            if queued and rules.fit_key(queued[0], mode, now) < rules.fit_key(f, mode, now):
                pairs.append((f, queued.pop(0)))
        return pairs

    def real_mode_on(self, day) -> str | None:
        """The last non-normal mode set on an ET date (not undone), even if the day ended on
        Normal - so "back to normal" at 2 PM still gets its re-pick the next morning."""
        start = datetime.combine(day, datetime.min.time(), tzinfo=ET)
        rows = self.conn.execute(
            "SELECT detail FROM events WHERE kind='mode_set' AND undone_by IS NULL AND at >= ? "
            "AND at < ? ORDER BY id", (db.to_db(start), db.to_db(start + timedelta(days=1))))
        names = [json.loads(r[0] or "{}").get("mode") for r in rows]
        real = [n for n in names if n and n != "normal"]
        return real[-1] if real else None

    def morning_repick(self, now: datetime | None = None) -> list[tuple[Task, Task]]:
        """First mode-free morning after a day with any mode: an untouched item a mode (or a
        swap) put in the three gives its slot back when a better queued item exists. One
        'repick' event (actor scheduler), undoable with "undo that". Returns (went back, came
        in) pairs."""
        now = now or self.clock.now()
        yesterday = now.astimezone(ET).date() - timedelta(days=1)
        ymode = self.real_mode_on(yesterday)
        if ymode is None or self.mode_record(now) is not None:
            return []
        pairs = self._filler_pairs(None, now)
        if not pairs:
            return []
        with self.conn:
            for out, into in pairs:
                self._set(out.id, status="queued", next_nag_at=_queued_nag(out, now))
                self._set(into.id, status="active",
                          next_nag_at=into.next_nag_at or into.due_at or now)
            eid = db.log_event(
                self.conn, None, "repick", now, actor="scheduler", after_mode=ymode,
                pairs=[[o.id, i.id] for o, i in pairs],
                side=[{"id": t.id, "before": _snap(t, ("status", "next_nag_at"))}
                      for pair in pairs for t in pair])
        self.last_event_id = eid
        return [(self.get(o.id), self.get(i.id)) for o, i in pairs]

    # ---- "new list" (added 2026-10-16) --------------------------------------------------
    def _started_recently(self, t: Task, now: datetime) -> bool:
        """Max tapped Do now / Start on it within the last hour: he's working on it."""
        acts = ",".join("?" * len(MAX_ACTORS))
        row = self.conn.execute(
            f"SELECT 1 FROM events WHERE task_id=? AND kind='do_now' AND undone_by IS NULL "
            f"AND actor IN ({acts}) AND at >= ? LIMIT 1",
            (t.id, *MAX_ACTORS, db.to_db(now - STARTED_PIN))).fetchone()
        return row is not None

    def new_list(self, actor: str | None = None) -> ModeResult:
        """'new list': re-rank right now instead of waiting for the next hourly message or the
        8 AM post. Active + queued items are ranked together (today's day mode applied, nothing
        swapped out today); the best become the three, the rest go back to the queue with no
        snooze used (a deadline within 48 h keeps it in the hourly message). An item started
        with Do now / Start in the last hour keeps its slot. One 'relisted' event; Undo puts
        the previous three back. Stored as a 'repick' row with via='new_list' (the events
        table's CHECK list has no new kind, so no schema migration is needed)."""
        now = self.clock.now()
        mode = self.current_mode(now)
        away = self.swapped_out_today(now)
        active = self.by_status("active")
        pinned = [t for t in active if self._started_recently(t, now)]
        pin_ids = {t.id for t in pinned}
        pool = [t for t in active if t.id not in pin_ids] + \
            [q for q in self.by_status("queued") if q.id not in away]
        budget = mode.total_minutes if mode is not None else None
        used = sum(rules.needs(t)[2] for t in pinned)
        target: list[Task] = []
        for t in rules.fit_rank(pool, mode, now):
            if len(pinned) + len(target) >= self.cap:
                break
            m = rules.needs(t)[2]
            if budget and t.status == "queued" and used + m > budget:
                continue
            target.append(t)
            used += m
        keep = pin_ids | {t.id for t in target}
        out = rules.rank([t for t in active if t.id not in keep], now)
        into = [t for t in target if t.status == "queued"]
        eid = None
        if out or into:
            with self.conn:
                for t in out:
                    self._set(t.id, status="queued", next_nag_at=_queued_nag(t, now))
                for t in into:
                    self._set(t.id, status="active",
                              next_nag_at=t.next_nag_at or t.due_at or now)
                eid = db.log_event(
                    self.conn, None, "repick", now, actor=actor, via="new_list",
                    mode=mode.name if mode is not None else None,
                    out=[t.id for t in out], into=[t.id for t in into],
                    side=[{"id": t.id, "before": _snap(t, ("status", "next_nag_at"))}
                          for t in out + into])
            self.last_event_id = eid
        three = rules.fit_rank(self.by_status("active"), mode, now) + \
            [t for t in rules.rank(self.by_status("active"), now) if not rules.fits(t, mode)]
        head = "New list" + (f" ({rules.MODE_LABELS.get(mode.name, 'today').lower()} filter on)"
                             if mode is not None else "") + ", ranked just now:"
        lines = [head]
        for i, t in enumerate(three, 1):
            due = f", due {fmt_et(t.due_at)}" if t.due_at else ""
            tag = " (started - kept)" if t.id in pin_ids else \
                (" (new)" if t.id in {x.id for x in into} else "")
            lines.append(f"{i}. #{t.id} {t.title[:70]} (~{_mins(t)}{due}){tag}")
        if not three:
            lines.append("Nothing fits right now - the queue is empty"
                         + (" or nothing in it fits today's mode." if mode is not None else "."))
        if out:
            lines.append("Back to the queue (no snooze used): " + ", ".join(
                f"#{t.id} {t.title[:50]}" for t in out) + ".")
        if not out and not into:
            lines.append("Same three as before - they're still the best picks.")
        return ModeResult("\n".join(lines), eid, [self.get(t.id) for t in out],
                          [self.get(t.id) for t in into], three, same=eid is None)

    # ---- swap / waiting / start / did / split (Phase 2.1) ---------------------------------
    def long_swaps(self, task_id: int) -> int:
        rows = self.conn.execute("SELECT detail FROM events WHERE task_id=? AND kind='swapped' "
                                 "AND undone_by IS NULL", (task_id,)).fetchall()
        return sum(1 for r in rows if json.loads(r[0] or "{}").get("reason") == "long")

    def swap_candidates(self, t: Task, reason: str, now: datetime) -> list[Task]:
        """Queued items that fit today and satisfy the swap reason, best first."""
        mode = self.current_mode(now)
        away = self.swapped_out_today(now) | {t.id}
        place, energy, minutes = rules.needs(t)
        cands = [c for c in self.by_status("queued") if c.id not in away]
        if reason == "long":
            cands = [c for c in cands if rules.needs(c)[2] < minutes]
        elif reason == "out":
            cands = [c for c in cands if rules.needs(c)[0] != "out"]
        elif reason == "energy":
            want = "light" if t.energy is None else ("light" if energy == "focused" else "focused")
            cands = [c for c in cands if rules.needs(c)[1] == want]
        return rules.fit_rank(cands, mode, now)

    def swap(self, task_id: int, reason: str, actor: str | None = None,
             until: datetime | None = None) -> SwapResult:
        """Swap one of the three for something that fits the reason. 'waiting' parks it as
        waiting on someone (check-in `until`, default +3 days). Two swaps per item, ever."""
        if reason not in rules.SWAP_REASONS:
            raise ValueError(f"bad swap reason {reason!r}")
        t = self._open(task_id)
        if t.status != "active":
            raise NotActive(f"#{t.id} isn't one of your three - only those can be swapped.")
        if t.swap_count >= rules.MAX_SWAPS:
            return SwapResult(t, forced=True, long_swaps=self.long_swaps(t.id))
        now = self.clock.now()
        cands = self.swap_candidates(t, reason, now)
        rep = cands[0] if cands else None
        if rep is None and reason != "waiting":
            self.log(t.id, "chat", actor=actor, action="swap_nothing_fits", reason=reason)
            return SwapResult(t, nothing_fits=True, long_swaps=self.long_swaps(t.id))
        cols = ("status", "next_nag_at", "swap_count", "waiting_until", "place", "guessed")
        with self.conn:
            if reason == "waiting":
                wu = until or now + rules.WAIT_DEFAULT
                self._set(t.id, status="waiting", waiting_until=wu, next_nag_at=wu,
                          swap_count=t.swap_count + 1)
            else:
                extra = {}
                if reason == "out":        # Max just told us it needs going out
                    g = [x for x in t.guessed if x != "place"]
                    extra = {"place": "out", "guessed": json.dumps(g) if g else None}
                self._set(t.id, status="queued", next_nag_at=_queued_nag(t, now),
                          swap_count=t.swap_count + 1, **extra)
            eid = db.log_event(self.conn, t.id, "swapped", now, actor=actor, reason=reason,
                               replacement_id=rep.id if rep else None,
                               until=until if reason == "waiting" else None,
                               before=_snap(t, cols))
        self.last_event_id = eid
        if rep is not None:
            got = self.promote_queued(cause_id=eid, actor=actor, only=[rep.id], via="swap")
            rep = got[0] if got else None
        self.last_event_id = eid
        return SwapResult(self.get(t.id), rep, eid, long_swaps=self.long_swaps(t.id))

    def set_waiting(self, task_id: int, until: datetime | None = None,
                    actor: str | None = None) -> tuple[Task, list[Task]]:
        """Blocked on someone else: no slot, one check-in ping at `until` (default +3 days)."""
        t = self._open(task_id)
        now = self.clock.now()
        wu = until or now + rules.WAIT_DEFAULT
        with self.conn:
            self._set(t.id, status="waiting", waiting_until=wu, next_nag_at=wu)
            eid = db.log_event(self.conn, t.id, "waiting", now, actor=actor, until=wu,
                               before=_snap(t, ("status", "next_nag_at", "waiting_until")))
        self.last_event_id = eid
        promoted = self.promote_queued(cause_id=eid, actor=actor) if t.status == "active" else []
        self.last_event_id = eid
        return self.get(t.id), promoted

    def wait_more_count(self, task_id: int) -> int:
        """'Wait more' extensions since the item last started waiting."""
        n = 0
        for r in self.conn.execute("SELECT kind, detail FROM events WHERE task_id=? AND kind IN "
                                   "('waiting','swapped') AND undone_by IS NULL ORDER BY id",
                                   (task_id,)):
            n = n + 1 if json.loads(r[1] or "{}").get("extended") else 0
        return n

    def wait_more(self, task_id: int, actor: str | None = None) -> Task:
        t = self._open(task_id)
        if t.status != "waiting":
            raise CantDo(f"#{t.id} isn't waiting on anyone.")
        if self.wait_more_count(t.id) >= rules.MAX_WAIT_MORE:
            raise CantDo(f"#{t.id} has been extended twice already - pick a day, mark it "
                         "got it, or kill it.")
        now = self.clock.now()
        wu = max(t.waiting_until or now, now) + rules.WAIT_DEFAULT
        with self.conn:
            self._set(t.id, waiting_until=wu, next_nag_at=wu)
            self.last_event_id = db.log_event(
                self.conn, t.id, "waiting", now, actor=actor, until=wu, extended=True,
                before=_snap(t, ("next_nag_at", "waiting_until")))
        return self.get(t.id)

    def resume_waiting(self, task_id: int, actor: str | None = None) -> tuple[Task, list[Task]]:
        """'Got it': back to the queue, due now; it takes a free slot by rank if there is one."""
        t = self._open(task_id)
        if t.status != "waiting":
            raise CantDo(f"#{t.id} isn't waiting on anyone.")
        now = self.clock.now()
        with self.conn:
            self._set(t.id, status="queued", waiting_until=None, next_nag_at=now)
            eid = db.log_event(self.conn, t.id, "resumed", now, actor=actor,
                               before=_snap(t, ("status", "next_nag_at", "waiting_until")))
        self.last_event_id = eid
        promoted = self.promote_queued(cause_id=eid, actor=actor)
        self.last_event_id = eid
        return self.get(t.id), promoted

    def due_checkins(self, now: datetime | None = None) -> list[Task]:
        """Waiting items whose check-in moment has come and that were not pinged for it yet -
        and, if Max left the check-in unanswered, again once a week after the last ping
        (review r1 fix 6: a forgotten waiting item is not a Sunday-only surprise)."""
        now = now or self.clock.now()
        out = []
        for t in self.by_status("waiting"):
            if t.waiting_until is None or t.waiting_until > now:
                continue
            last = self.conn.execute("SELECT MAX(at) FROM events WHERE task_id=? AND "
                                     "kind='checkin_sent' AND at >= ?",
                                     (t.id, db.to_db(t.waiting_until))).fetchone()[0]
            if last is None or db.from_db(last) <= now - rules.CHECKIN_REPEAT:
                out.append(t)
        return out

    def start(self, task_id: int, actor: str | None = None) -> StartResult:
        """'Start' on a best-fit suggestion: like Do now. A queued item takes a slot; with three
        active the lowest-ranked non-urgent one goes back to the queue (Undo restores both)."""
        t = self._open(task_id)
        if t.status not in ("active", "queued"):
            raise CantDo(f"#{t.id} is {t.status} - it can't be started from here.")
        now = self.clock.now()
        demote = None
        if t.status == "queued" and self.active_count() >= self.cap:
            movable = [a for a in self.by_status("active") if not rules.urgent(a, now)]
            if not movable:
                raise CantDo("All three are due within 48 hours - finish or move one first.")
            demote = rules.rank(movable, now)[-1]
        with self.conn:
            if demote is not None:
                self._set(demote.id, status="queued", next_nag_at=_queued_nag(demote, now))
            self._set(t.id, status="active", next_nag_at=now + timedelta(hours=1))
            eid = db.log_event(
                self.conn, t.id, "do_now", now, actor=actor, via="start",
                before=_snap(t, ("status", "next_nag_at")),
                side=[{"id": demote.id, "before": _snap(demote, ("status", "next_nag_at"))}]
                if demote else [])
        self.last_event_id = eid
        return StartResult(self.get(t.id), eid, self.get(demote.id) if demote else None)

    def log_done(self, title: str, area: str | None = None, actor: str | None = None,
                 notes: str | None = None, guessed: list[str] | None = None,
                 info: dict | None = None) -> Task:
        """'did: X' - something finished off the list. Born done; counts in today's stats.
        Undo target = the 'created' row (the item is then marked killed, history kept)."""
        now = self.clock.now()
        area = normalize(area)
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO tasks(title, area, status, snooze_count, next_nag_at, created_at, "
                "closed_at, source, notes, guessed) VALUES (?,?,'done',0,NULL,?,?,'inbox',?,?)",
                (title, area, db.to_db(now), db.to_db(now), notes,
                 json.dumps(guessed) if guessed else None))
            tid = cur.lastrowid
            cid = db.log_event(self.conn, tid, "created", now, actor=actor, status="done",
                               area=area, source="inbox", did=True, **(info or {}))
            db.log_event(self.conn, tid, "done", now, actor=actor, cause_id=cid, was="done",
                         did=True)
        self.last_event_id = cid
        return self.get(tid)

    def split(self, parent_id: int, step: str, minutes: int,
              actor: str | None = None) -> tuple[Task, Task, int]:
        """Split off a first step (<= 20 min). The step takes the parent's slot; the parent goes
        back to the queue, linked (parent_id). Returns (child, parent, split event id)."""
        p = self._open(parent_id)
        if p.status not in ("active", "queued"):
            raise CantDo(f"#{p.id} is {p.status} - it can't be split from here.")
        now = self.clock.now()
        minutes = int(min(max(minutes, 5), rules.SPLIT_STEP_MAX_MIN))
        was_active = p.status == "active"
        child_status = "active" if was_active or self.active_count() < self.cap else "queued"
        with self.conn:
            if was_active:
                self._set(p.id, status="queued", next_nag_at=_queued_nag(p, now))
            cur = self.conn.execute(
                "INSERT INTO tasks(title, area, status, estimate_minutes, snooze_count, "
                "next_nag_at, created_at, source, notes, place, energy, parent_id) "
                "VALUES (?,?,?,?,0,?,?,?,?,?,'light',?)",
                (step, p.area, child_status, minutes,
                 db.to_db(now) if child_status == "active" else None, db.to_db(now), p.source,
                 f"Step of #{p.id}", p.place, p.id))
            cid = cur.lastrowid
            sid = db.log_event(self.conn, p.id, "split", now, actor=actor, child=cid, step=step,
                               minutes=minutes, before=_snap(p, ("status", "next_nag_at")))
            db.log_event(self.conn, cid, "created", now, actor=actor, cause_id=sid,
                         status=child_status, area=p.area, via="split", parent=p.id)
        self.last_event_id = sid
        return self.get(cid), self.get(p.id), sid

    def set_needs_guess(self, task_id: int, place: str | None, energy: str | None,
                        actor: str = "qwen") -> Task | None:
        """Backfill: write a guessed place/energy where the item has none yet (never over a
        value Max set). One 'needs_set' row per item, marked as a guess."""
        t = self.get(task_id)
        if t is None or t.status not in OPEN:
            return None
        clean = {}
        if t.place is None and place in rules.PLACES:
            clean["place"] = place
        if t.energy is None and energy in rules.ENERGIES:
            clean["energy"] = energy
        if not clean:
            return t
        guessed = list(dict.fromkeys(t.guessed + list(clean)))
        now = self.clock.now()
        with self.conn:
            self._set(t.id, **clean, guessed=json.dumps(guessed))
            db.log_event(self.conn, t.id, "needs_set", now, actor=actor, after=clean, guess=True,
                         before=_snap(t, tuple(clean) + ("guessed",)))
        return self.get(t.id)

    def _close(self, task_id: int, status: str, actor: str | None) -> tuple[Task, list[Task]]:
        t = self._open(task_id)
        now = self.clock.now()
        cols = ("status", "closed_at", "next_nag_at") + \
            (("waiting_until",) if t.waiting_until is not None else ())
        with self.conn:
            self._set(task_id, status=status, closed_at=now, next_nag_at=None,
                      **({"waiting_until": None} if t.waiting_until is not None else {}))
            eid = db.log_event(self.conn, task_id, status, now, actor=actor, was=t.status,
                               snoozes=t.snooze_count, before=_snap(t, cols))
        self.last_event_id = eid
        promoted = self.promote_queued(cause_id=eid, actor=actor) if t.status == "active" else []
        return self.get(task_id), promoted

    def mark_done(self, task_id: int, actor: str | None = None) -> tuple[Task, list[Task]]:
        return self._close(task_id, "done", actor)

    def kill(self, task_id: int, actor: str | None = None) -> tuple[Task, list[Task]]:
        return self._close(task_id, "killed", actor)

    def snooze(self, task_id: int, choice: str, explicit: datetime | None = None,
               actor: str | None = None) -> Task:
        t = self._open(task_id)
        self._not_waiting(t)
        if choice not in rules.snooze_options(t.snooze_count) or choice not in rules.SNOOZE_CHOICES:
            raise LadderError(f"snooze '{choice}' not allowed after {t.snooze_count} snoozes")
        now = self.clock.now()
        until = rules.resolve_snooze(choice, now, explicit)
        with self.conn:
            self._set(task_id, snooze_count=t.snooze_count + 1, next_nag_at=until)
            self.last_event_id = db.log_event(
                self.conn, task_id, "snoozed", now, actor=actor, choice=choice, until=until,
                count=t.snooze_count + 1, before=_snap(t, ("snooze_count", "next_nag_at")))
        return self.get(task_id)

    def do_now(self, task_id: int, actor: str | None = None) -> Task:
        """Max commits to doing it now. It comes back in an hour if still open."""
        t = self._open(task_id)
        self._not_waiting(t)
        now = self.clock.now()
        with self.conn:
            self._set(task_id, next_nag_at=now + timedelta(hours=1))
            self.last_event_id = db.log_event(self.conn, task_id, "do_now", now, actor=actor,
                                              before=_snap(t, ("next_nag_at",)))
        return self.get(task_id)

    def reschedule(self, task_id: int, new_due: datetime,
                   actor: str | None = None) -> tuple[Task, list[Task]]:
        """'Pick a day'. A new day after today frees the active slot (reschedule-out)."""
        t = self._open(task_id)
        now = self.clock.now()
        moves_out = t.status == "active" and new_due.date() > now.date()
        # a waiting item that gets a day is no longer waiting: it is a queued item with a date
        status = "queued" if moves_out or t.status == "waiting" else t.status
        extra = {"waiting_until": None} if t.status == "waiting" else {}
        with self.conn:
            self._set(task_id, due_at=new_due, next_nag_at=new_due, snooze_count=0,
                      status=status, **extra)
            eid = db.log_event(self.conn, task_id, "rescheduled", now, actor=actor,
                               old_due=t.due_at, new_due=new_due, freed_slot=moves_out,
                               before=_snap(t, ("due_at", "next_nag_at", "snooze_count", "status")
                                            + tuple(extra)))
        self.last_event_id = eid
        promoted = self.promote_queued(exclude=task_id, cause_id=eid, actor=actor) if moves_out else []
        return self.get(task_id), promoted

    def set_fields(self, task_id: int, actor: str | None = None, **fields) -> Task:
        """Correct estimate_minutes / payoff / likelihood / area. A corrected field is no longer
        'guessed'. One event: estimated, prioritized or area_set (mixed -> prioritized)."""
        t = self._open(task_id)
        bad = set(fields) - set(FIELD_KIND)
        if bad or not fields:
            raise ValueError(f"cannot set {sorted(bad) or 'nothing'}")
        clean = {}
        for k, v in fields.items():
            if k == "area":
                clean[k] = normalize(v)
            elif k in ("place", "energy"):
                allowed = rules.PLACES if k == "place" else rules.ENERGIES
                if v not in allowed:
                    raise ValueError(f"{k} must be one of {', '.join(allowed)}")
                clean[k] = v
            elif k == "estimate_minutes":
                clean[k] = int(min(max(round(float(v)), 1), 100_000))
            elif k == "payoff":
                clean[k] = float(min(max(round(float(v)), 1), 5))
            else:
                clean[k] = float(min(max(float(v), 0.0), 1.0))
        kinds = {FIELD_KIND[k] for k in clean}
        kind = kinds.pop() if len(kinds) == 1 else "prioritized"
        guessed = [g for g in t.guessed if g not in clean]
        now = self.clock.now()
        with self.conn:
            self._set(task_id, **clean, guessed=json.dumps(guessed) if guessed else None)
            self.last_event_id = db.log_event(
                self.conn, task_id, kind, now, actor=actor, after=clean,
                before=_snap(t, tuple(clean) + ("guessed",)))
        return self.get(task_id)

    def shift_after(self, until: datetime, actor: str | None = None) -> tuple[list[Task], int]:
        """"I'm slammed until 4": every open item that would nag before `until` waits until then.
        Due dates and snooze counts are untouched. Returns (moved tasks, event id)."""
        now = self.clock.now()
        movers = [t for t in self.by_status("active", "queued")
                  if t.next_nag_at is not None and t.next_nag_at < until]
        with self.conn:
            for t in movers:
                self._set(t.id, next_nag_at=until)
            eid = db.log_event(self.conn, None, "shifted", now, actor=actor, until=until,
                               tasks=[{"id": t.id, "before": _snap(t, ("next_nag_at",))}
                                      for t in movers])
        self.last_event_id = eid
        return [self.get(t.id) for t in movers], eid

    def record_nags(self, tasks: list[Task], kind: str = "nag_sent") -> None:
        now = self.clock.now()
        with self.conn:
            for t in tasks:
                db.log_event(self.conn, t.id, kind, now, actor="scheduler", snoozes=t.snooze_count)

    # ---- undo ----------------------------------------------------------------------------
    def _later_change(self, task_id: int, after_id: int, own_cause: int | None = None):
        """First event after `after_id` that changed task_id and is still in force. Rows caused
        by `own_cause` (its promotions, the 'done' of a did:, a split's step) are part of the
        change being undone, so they don't count; nor does a place/energy GUESS by the local AI
        (it only fills empty columns). Events that list several tasks ('tasks' of a shift,
        'side' of a day mode / re-pick / Start) count for each task they list."""
        marks = ",".join("?" * len(NOT_A_CHANGE))
        rows = self.conn.execute(
            f"SELECT * FROM events WHERE id > ? AND undone_by IS NULL AND kind NOT IN ({marks}) "
            f"AND (task_id = ? OR task_id IS NULL OR instr(detail, '\"side\"') > 0) ORDER BY id",
            (after_id, *NOT_A_CHANGE, task_id))
        for r in rows:
            if own_cause is not None and r["cause_id"] == own_cause:
                continue
            if r["kind"] == "needs_set" and r["actor"] == "qwen":
                continue
            if r["task_id"] != task_id:
                d = db.detail_of(r)
                if task_id not in [x.get("id") for x in d.get("tasks", []) + d.get("side", [])]:
                    continue
            return r
        return None

    def latest_undoable(self, now: datetime | None = None) -> int | None:
        """Newest change Max made (button, chat or Siri) that can still be undone."""
        now = now or self.clock.now()
        kinds = ",".join("?" * len(UNDOABLE))
        actors = ",".join("?" * len(MAX_ACTORS))
        row = self.conn.execute(
            f"SELECT id FROM events WHERE kind IN ({kinds}) "
            f"AND (actor IN ({actors}) OR kind = 'repick') AND cause_id IS NULL "
            f"AND undone_by IS NULL AND at >= ? ORDER BY id DESC LIMIT 1",
            (*UNDOABLE, *MAX_ACTORS, db.to_db(now - UNDO_WINDOW))).fetchone()
        return row[0] if row else None

    def undo(self, event_id: int, actor: str | None = None) -> UndoResult:
        """Exactly reverse one change (and the promotions it caused, if untouched since)."""
        now = self.clock.now()
        ev = db.get_event(self.conn, event_id)
        if ev is None:
            raise UndoRefused("I can't find that change any more.")
        if ev["cause_id"] is not None and ev["kind"] in ("done", "created"):
            # part of a bigger change (the 'done' of a did:, a split's step): undo that one
            event_id = ev["cause_id"]
            ev = db.get_event(self.conn, event_id)
        kind = ev["kind"]
        if kind == "undone":
            raise UndoRefused("Can't undo an undo - just tell me what you want.")
        if kind not in UNDOABLE:
            raise UndoRefused(f"That ({kind.replace('_', ' ')}) isn't a change I can undo.")
        if ev["undone_by"] is not None:
            raise UndoRefused("That was already undone.")
        if db.from_db(ev["at"]) < now - UNDO_WINDOW:
            raise UndoRefused("That's more than 48 hours old, so Undo has expired. "
                              "Tell me what you want instead.")
        d = db.detail_of(ev)
        if kind == "shifted":
            targets = [(x["id"], x.get("before")) for x in d.get("tasks", [])]
        else:
            targets = [(ev["task_id"], d.get("before"))] if ev["task_id"] is not None else []
            targets += [(x["id"], x.get("before")) for x in d.get("side", [])]
        mode_meta = None
        if kind == "mode_set":
            key = MODE_KEY + str(d.get("date"))
            rec = self.meta_json(key) or {}
            if rec.get("event_id") != event_id:
                raise UndoRefused("The day mode was changed again after that - just tell me "
                                  "which mode you want.")
            mode_meta = (key, (d.get("before") or {}).get("meta"))
        caused = self.conn.execute(
            "SELECT * FROM events WHERE cause_id=? AND undone_by IS NULL ORDER BY id",
            (event_id,)).fetchall()
        for c in caused:
            if c["kind"] == "created" and self._later_change(c["task_id"], c["id"]) is not None:
                raise UndoRefused(f"Can't undo the split: the first step #{c['task_id']} was "
                                  "already changed since. Tell me what you want instead.")
        for tid, before in targets:
            if kind != "created" and before is None:
                raise UndoRefused("That change was made before Undo existed, so I can't reverse it.")
            later = self._later_change(tid, event_id, own_cause=event_id)
            if later is not None:
                t = self.get(tid)
                raise UndoRefused(
                    f"Can't undo #{tid}'s {WHAT[kind]}: it was {PAST.get(later['kind'], later['kind'])} "
                    f"at {fmt_et(db.from_db(later['at']))} after that. Tell me what you want instead."
                    if t else "That item no longer exists.")

        notes: list[str] = []
        reversed_ids = [event_id]
        was_active = False
        with self.conn:
            for tid, before in targets:
                if kind == "created":
                    t = self.get(tid)
                    was_active = t.status == "active"
                    self._set(tid, status="killed", closed_at=now, next_nag_at=None)
                else:
                    self._set(tid, **before)
            if mode_meta is not None:
                if mode_meta[1] is None:
                    self.conn.execute("DELETE FROM meta WHERE key = ?", (mode_meta[0],))
                else:
                    db.meta_set(self.conn, mode_meta[0], mode_meta[1])
            target_ids = {tid for tid, _ in targets}
            for p in caused:
                if p["kind"] != "promoted":
                    if p["kind"] == "created":     # a split's first step: removed with it
                        ct = self.get(p["task_id"])
                        if ct.status in OPEN:
                            self._set(ct.id, status="killed", closed_at=now, next_nag_at=None)
                        notes.append(f"The step #{ct.id} is removed (marked killed).")
                    reversed_ids.append(p["id"])   # e.g. the 'done' of a did:
                    continue
                if p["task_id"] in target_ids:     # the undone item itself (add, then promoted)
                    reversed_ids.append(p["id"])
                    continue
                pt = self.get(p["task_id"])
                if self._later_change(p["task_id"], p["id"]) is None and pt.status == "active":
                    self._set(p["task_id"], **db.detail_of(p).get("before", {"status": "queued"}))
                    reversed_ids.append(p["id"])
                    notes.append(f"#{pt.id} goes back to the queue.")
                else:
                    notes.append(f"#{pt.id} stays active because you already changed it since.")
            uid = db.log_event(self.conn, ev["task_id"], "undone", now, actor=actor,
                               event_id=event_id, undid=kind, reversed=reversed_ids)
            marks = ",".join("?" * len(reversed_ids))
            self.conn.execute(f"UPDATE events SET undone_by = ? WHERE id IN ({marks})",
                              (uid, *reversed_ids))
        self.last_event_id = uid
        promoted = []
        if was_active or self.active_count() < self.cap:
            promoted = self.promote_queued(cause_id=uid, actor=actor)
        notes += [f"Up next: #{p.id} {p.title}." for p in promoted]
        if kind == "shifted":
            head = f"Undid the shift - {len(targets)} item(s) are back on their old times."
        elif kind == "mode_set":
            rec = self.mode_record(now)
            was = rules.MODE_LABELS.get((rec or {}).get("mode"), "no mode") if rec else "no mode"
            head = f"Undid the day mode - today is back to {was.lower()}."
            back = [f"#{tid}" for tid in d.get("demoted", [tid for tid, _ in targets])]
            if back:
                head += f" {', '.join(back)} {'is' if len(back) == 1 else 'are'} back in your three."
            if d.get("repicked"):
                head += " The re-pick that came with it is reversed too."
        elif kind == "repick" and d.get("via") == "new_list":
            head = "Undid the new list - your previous three are back."
        elif kind == "repick":
            head = "Undid the re-pick - yesterday's three are back."
        else:
            t = self.get(ev["task_id"])
            state = "removed (marked killed, history kept)" if kind == "created" else \
                f"back to {t.status}" + (f", next reminder {fmt_et(t.next_nag_at)}"
                                         if t.next_nag_at and t.status in ("active", "queued") else "")
            head = f"Undid the {WHAT[kind]}: #{t.id} {t.title} is {state}."
        return UndoResult(" ".join([head] + notes), uid, reversed_ids, promoted)

    # ---- split offers and "didn't fit" (Phase 2.1) ----------------------------------------
    def mode_demoted_days(self, task_id: int, now: datetime | None = None,
                          days: int = 7) -> set:
        """ET dates in the last `days` on which a day mode (or re-pick) set this item aside."""
        now = now or self.clock.now()
        rows = self.conn.execute(
            "SELECT at, detail FROM events WHERE kind IN ('mode_set','repick') AND "
            "undone_by IS NULL AND at >= ?", (db.to_db(now - timedelta(days=days)),)).fetchall()
        out = set()
        for r in rows:
            d = json.loads(r["detail"] or "{}")
            if "pairs" in d or d.get("via") == "new_list":
                continue      # a re-pick / new list re-ranks - that is not "didn't fit"
            outs = set(d["demoted"]) if "demoted" in d else \
                {x.get("id") for x in d.get("side", [])}
            if task_id in outs:
                out.add(db.from_db(r["at"]).date())
        return out

    def _last_touch(self, task_id: int) -> datetime | None:
        marks = ",".join("?" * len(NOT_A_CHANGE))
        row = self.conn.execute(
            f"SELECT MAX(at) FROM events WHERE task_id=? AND kind NOT IN ({marks}) "
            f"AND NOT (kind='needs_set' AND actor='qwen')",
            (task_id, *NOT_A_CHANGE)).fetchone()
        return db.from_db(row[0]) if row and row[0] else None

    def split_candidate(self, now: datetime | None = None) -> Task | None:
        """At most one item to offer a split for this morning: one of the three estimated over
        an hour and untouched for 2+ days, or an item a day mode set aside on 2+ days this
        week. Never an item with an open step, a pending offer, or declined this week."""
        now = now or self.clock.now()
        children = {r[0] for r in self.conn.execute(
            "SELECT parent_id FROM tasks WHERE parent_id IS NOT NULL AND status IN "
            "('active','queued','waiting')")}

        def eligible(t: Task) -> bool:
            if t.id in children or rules.needs(t)[2] <= rules.SPLIT_STEP_MAX_MIN:
                return False
            for key in (f"split_offer:{t.id}", f"split_declined:{t.id}"):
                rec = self.meta_json(key)
                if rec and rec.get("at") and \
                        db.from_db(rec["at"]) > now - (timedelta(hours=24) if "offer" in key
                                                       else timedelta(days=7)):
                    return False
            return True

        for t in rules.rank(self.by_status("active"), now):
            last = self._last_touch(t.id)
            if rules.needs(t)[2] > rules.SPLIT_LONG_MIN and last is not None and \
                    last <= now - rules.SPLIT_UNTOUCHED and eligible(t):
                return t
        for t in rules.rank(self.by_status("queued"), now):
            if len(self.mode_demoted_days(t.id, now)) >= 2 and eligible(t):
                return t
        return None

    def never_fit(self, now: datetime | None = None) -> list[Task]:
        """Queued items a day mode set aside on 3+ days of the last 7 (Sunday review line)."""
        now = now or self.clock.now()
        return [t for t in self.by_status("queued")
                if len(self.mode_demoted_days(t.id, now)) >= 3]

    # ---- scheduler memory ----------------------------------------------------------------
    def meta_get(self, key: str) -> str | None:
        return db.meta_get(self.conn, key)

    def meta_set(self, key: str, value: str | None) -> None:
        with self.conn:
            db.meta_set(self.conn, key, value)

    def meta_json(self, key: str) -> dict | None:
        raw = self.meta_get(key)
        try:
            return json.loads(raw) if raw else None
        except ValueError:
            return None


def describe(t: Task) -> str:
    """One line for Discord / the dry-run printout."""
    bits = [f"#{t.id} {t.title}", t.area]
    if t.due_at:
        bits.append(f"due {fmt_et(t.due_at)}")
    if t.snooze_count:
        bits.append(f"snoozed {t.snooze_count}x")
    if t.status == "waiting":
        bits.append("waiting" + (f", check-in {fmt_et(t.waiting_until)}" if t.waiting_until
                                 else ""))
    elif t.status != "active":
        bits.append(t.status)
    if t.parent_id:
        bits[0] += f" (step of #{t.parent_id})"
    return " - ".join(bits)
