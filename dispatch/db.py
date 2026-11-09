"""SQLite storage: connection, versioned migrations, row <-> Task conversion, event log."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .clock import ET, UTC

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

# Columns of the Phase 1 schema (v2). Kept separately so a v3 database can still be read by
# code that only knows these (see tests/test_migration.py - rollback safety).
V2_TASK_COLUMNS = ("id", "title", "area", "status", "due_at", "estimate_minutes", "payoff",
                   "likelihood", "snooze_count", "next_nag_at", "created_at", "closed_at",
                   "source", "notes")
TASK_COLUMNS = V2_TASK_COLUMNS + ("external_ref", "guessed", "place", "energy", "swap_count",
                                  "waiting_until", "parent_id")
_TIME_COLUMNS = {"due_at", "next_nag_at", "created_at", "closed_at", "waiting_until"}
BUSY_TIMEOUT_MS = 5000


def to_db(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        raise ValueError("refusing to store a timezone-naive datetime")
    return dt.astimezone(UTC).isoformat()


def from_db(s: str | None) -> datetime | None:
    return None if s is None else datetime.fromisoformat(s).astimezone(ET)


@dataclass
class Task:
    id: int
    title: str
    area: str
    status: str
    due_at: datetime | None
    estimate_minutes: int | None
    payoff: float | None
    likelihood: float | None
    snooze_count: int
    next_nag_at: datetime | None
    created_at: datetime
    closed_at: datetime | None
    source: str
    notes: str | None
    external_ref: str | None = None
    guessed: list[str] = field(default_factory=list)   # fields the local AI guessed
    place: str | None = None           # home / out / phone (None = not known yet)
    energy: str | None = None          # light / focused (None = not known yet)
    swap_count: int = 0                # swaps over the item's lifetime (never reset)
    waiting_until: datetime | None = None   # check-in moment of a 'waiting' item
    parent_id: int | None = None       # the item this one was split off from

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Task":
        keys = set(row.keys())
        vals = {c: row[c] for c in TASK_COLUMNS if c in keys}
        for c in _TIME_COLUMNS:
            if c in vals:
                vals[c] = from_db(vals[c])
        if vals.get("swap_count") is None and "swap_count" in vals:
            vals["swap_count"] = 0
        raw = vals.get("guessed")
        try:
            vals["guessed"] = [str(x) for x in json.loads(raw)] if raw else []
        except (ValueError, TypeError):
            vals["guessed"] = []
        return cls(**vals)


def connect(path: str | Path) -> sqlite3.Connection:
    """Open the database. busy_timeout lets the CLI and the bot share the file: a writer waits
    up to 5 s for the other instead of failing. WAL (write-ahead log) lets readers and one
    writer work at the same time. Switching to WAL needs a moment with no other user of the
    file; if the file is busy (e.g. the Phase 1 bot still holds it) we just carry on."""
    conn = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.OperationalError:
        pass
    return conn


def current_version(conn: sqlite3.Connection) -> int:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version ("
                 "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    return row[0] or 0


def migrate(conn: sqlite3.Connection, now: datetime) -> list[int]:
    """Apply every migrations/NNNN_*.sql newer than the stored version. Returns versions applied.

    Foreign keys are switched OFF while scripts run (a table rebuild drops and re-creates a
    table other rows point at; the PRAGMA is ignored inside a transaction, hence the commit
    first) and every reference is re-checked before the version is recorded."""
    have = current_version(conn)
    conn.commit()
    applied = []
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        for f in sorted(MIGRATIONS_DIR.glob("*.sql")):
            v = int(f.name.split("_", 1)[0])
            if v <= have:
                continue
            sql = f.read_text(encoding="utf-8")
            with conn:
                conn.executescript(sql)
            broken = conn.execute("PRAGMA foreign_key_check").fetchall()
            if broken:
                raise RuntimeError(f"migration {v:04d} broke a reference: {[tuple(b) for b in broken][:5]}")
            with conn:
                conn.execute("INSERT INTO schema_version(version, applied_at) VALUES (?, ?)",
                             (v, to_db(now)))
            applied.append(v)
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
    return applied


def get_task(conn: sqlite3.Connection, task_id: int) -> Task | None:
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return Task.from_row(row) if row else None


def get_task_by_ref(conn: sqlite3.Connection, ref: str) -> Task | None:
    row = conn.execute("SELECT * FROM tasks WHERE external_ref = ?", (ref,)).fetchone()
    return Task.from_row(row) if row else None


def tasks_where(conn: sqlite3.Connection, where: str = "1=1", params: tuple = ()) -> list[Task]:
    rows = conn.execute(f"SELECT * FROM tasks WHERE {where} ORDER BY id", params).fetchall()
    return [Task.from_row(r) for r in rows]


# Extra fields stamped on every event written in the current asyncio task / thread - used to
# tag each event with the Discord message that caused it ("msg"), in the SAME transaction as
# the change itself. A ContextVar is per asyncio task, so the minute loop or a button press
# running meanwhile is never tagged with someone else's message.
EVENT_CONTEXT: ContextVar[dict] = ContextVar("dispatch_event_context", default={})


@contextmanager
def event_context(**fields):
    token = EVENT_CONTEXT.set({**EVENT_CONTEXT.get(), **fields})
    try:
        yield
    finally:
        EVENT_CONTEXT.reset(token)


def events_for_message(conn: sqlite3.Connection, message_id: int) -> list[sqlite3.Row]:
    """Every event written while handling this Discord message (see EVENT_CONTEXT)."""
    return conn.execute("SELECT * FROM events WHERE detail IS NOT NULL AND "
                        "json_extract(detail, '$.msg') = ? ORDER BY id", (message_id,)).fetchall()


def log_event(conn: sqlite3.Connection, task_id: int | None, kind: str, at: datetime,
              actor: str | None = None, cause_id: int | None = None, **detail) -> int:
    """Append one row to the event log and return its id."""
    ctx = EVENT_CONTEXT.get()
    if ctx:
        detail = {**detail, **ctx}
    cur = conn.execute(
        "INSERT INTO events(task_id, kind, at, detail, actor, cause_id) VALUES (?, ?, ?, ?, ?, ?)",
        (task_id, kind, to_db(at), json.dumps(detail, default=str) if detail else None,
         actor, cause_id))
    return cur.lastrowid


def events_for(conn: sqlite3.Connection, task_id: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM events WHERE task_id = ? ORDER BY id", (task_id,)).fetchall()


def get_event(conn: sqlite3.Connection, event_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()


def detail_of(row: sqlite3.Row) -> dict:
    try:
        return json.loads(row["detail"]) if row["detail"] else {}
    except ValueError:
        return {}


def meta_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def meta_set(conn: sqlite3.Connection, key: str, value: str | None) -> None:
    conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
