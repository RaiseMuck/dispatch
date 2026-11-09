"""Dispatch CLI - operate the to-do list without Discord (for Claude sessions and scheduled tasks).

    python -m dispatch cli <command> [options] [--db PATH] [--json]

Output is ALWAYS exactly one JSON object on stdout (--json is accepted and ignored; logs go to
stderr). Exit codes: 0 ok, 1 refused / not found, 2 bad arguments. Errors look like
    {"ok": false, "error": "<code>", "message": "<plain words>"}
Safe to run while the bot is running (SQLite WAL + 5 s busy timeout; the bot re-reads the
database every minute). The CLI never migrates the database: if it is older than this code it
answers error "schema_old" - run `python -m dispatch migrate` (the switch script does).

A task object has every column - id, title, area, status, due_at, estimate_minutes, payoff,
likelihood, snooze_count, next_nag_at, created_at, closed_at, source, notes, external_ref,
guessed (list of fields the local AI guessed), place (home/out/phone), energy (light/focused),
swap_count, waiting_until (check-in of a waiting item), parent_id (split-off step) - with times as ISO-8601 in US Eastern, e.g.
"2026-10-03T11:00:00-04:00", plus "score" (rank score), "urgent" (due within 48 h) and "why".

Commands
  add TITLE [--due PHRASE_OR_ISO] [--area NAME] [--source claude|gmail|canvas|vault]
            [--external-ref REF] [--notes TEXT] [--payoff 1-5] [--likelihood 0-1]
            [--estimate MINUTES] [--place home|out|phone] [--energy light|focused]
            [--park] [--no-guess]
      -> {"ok": true, "created": true|false, "task": {...},
          "guess": {"model": str|null, "ms": int, "guessed": [...]}, "promoted": [{...}]}
      "promoted" = OTHER items this add moved into the active three (a free slot is filled
      by rank, so the best waiting item may move up instead of the new one).
      --source defaults to claude. --due tries ISO first (no offset = Eastern), then phrases
      like "Friday 3pm". A second add with the same --external-ref (e.g. a Gmail message id)
      adds nothing and returns the existing task with "created": false, whatever its status.
      Unless --no-guess, the local AI guesses area/payoff/likelihood/estimate/place/energy;
      flags you pass
      win over its guesses. --park files it as a parked idea (never nags).
  list [--status active,queued,parked,waiting,done,killed] [--area NAME] [--due-before ISO] [--limit N]
      -> {"ok": true, "count": n, "tasks": [...]}   (default: active,queued; best-ranked first)
  get ID                -> {"ok": true, "task": {...}, "events": [{id, kind, at, actor, detail}]}
  done ID | kill ID     -> {"ok": true, "task": {...}, "promoted": [{...}], "event_id": n}
  reschedule ID --due PHRASE_OR_ISO
                        -> {"ok": true, "task": {...}, "promoted": [{...}], "event_id": n}
  estimate ID --minutes N               -> {"ok": true, "task": {...}, "event_id": n}
  set ID [--payoff 1-5] [--likelihood 0-1] [--area NAME] [--estimate MINUTES]
         [--place home|out|phone] [--energy light|focused]
                                        -> {"ok": true, "task": {...}, "event_id": n}
  undo EVENT_ID         -> {"ok": true, "reversed": [event ids], "message": "..."}
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from . import db, qwen, rules
from .areas import AREAS, UNSORTED, normalize
from .clock import ET, SystemClock
from .config import load_settings
from .parser import parse_when
from .service import NotOpen, TaskService, UndoRefused

SOURCES = ("claude", "gmail", "canvas", "vault")
STATUSES = ("active", "queued", "parked", "waiting", "done", "killed")


class BadArgs(Exception):
    pass


class Refused(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


class _Parser(argparse.ArgumentParser):
    def error(self, message):              # never print usage text on stdout, never exit
        raise BadArgs(message)


def _payoff(v: str) -> int:
    try:
        x = int(v)
    except ValueError:
        raise argparse.ArgumentTypeError("payoff must be a whole number 1-5")
    if not 1 <= x <= 5:
        raise argparse.ArgumentTypeError("payoff must be 1-5")
    return x


def _likelihood(v: str) -> float:
    try:
        x = float(v)
    except ValueError:
        raise argparse.ArgumentTypeError("likelihood must be a number 0-1")
    if not 0.0 <= x <= 1.0:
        raise argparse.ArgumentTypeError("likelihood must be 0-1")
    return x


def _minutes(v: str) -> int:
    try:
        x = int(v)
    except ValueError:
        raise argparse.ArgumentTypeError("minutes must be a whole number")
    if x <= 0:
        raise argparse.ArgumentTypeError("minutes must be positive")
    return x


def build_parser() -> argparse.ArgumentParser:
    # --db / --json work before or after the command. The copy on the subcommands has no
    # default so it cannot overwrite a value given before the command.
    top = _Parser(add_help=False)
    top.add_argument("--db", type=Path, help="database file (default: dispatch.db of this install)")
    top.add_argument("--json", action="store_true", help="accepted for clarity; output is always JSON")
    common = _Parser(add_help=False)
    common.add_argument("--db", type=Path, default=argparse.SUPPRESS)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    ap = _Parser(prog="python -m dispatch cli", parents=[top])
    sub = ap.add_subparsers(dest="cmd", required=True, parser_class=_Parser)
    p = sub.add_parser("add", parents=[common])
    p.add_argument("title")
    p.add_argument("--due")
    p.add_argument("--area")
    p.add_argument("--source", choices=SOURCES, default="claude")
    p.add_argument("--external-ref")
    p.add_argument("--notes")
    p.add_argument("--payoff", type=_payoff)
    p.add_argument("--likelihood", type=_likelihood)
    p.add_argument("--estimate", type=_minutes)
    p.add_argument("--place", choices=rules.PLACES)
    p.add_argument("--energy", choices=rules.ENERGIES)
    p.add_argument("--park", action="store_true")
    p.add_argument("--no-guess", action="store_true")
    p = sub.add_parser("list", parents=[common])
    p.add_argument("--status", default="active,queued")
    p.add_argument("--area")
    p.add_argument("--due-before")
    p.add_argument("--limit", type=int)
    for name in ("get", "done", "kill"):
        sub.add_parser(name, parents=[common]).add_argument("id", type=int)
    p = sub.add_parser("reschedule", parents=[common])
    p.add_argument("id", type=int)
    p.add_argument("--due", required=True)
    p = sub.add_parser("estimate", parents=[common])
    p.add_argument("id", type=int)
    p.add_argument("--minutes", type=_minutes, required=True)
    p = sub.add_parser("set", parents=[common])
    p.add_argument("id", type=int)
    p.add_argument("--payoff", type=_payoff)
    p.add_argument("--likelihood", type=_likelihood)
    p.add_argument("--area")
    p.add_argument("--estimate", type=_minutes)
    p.add_argument("--place", choices=rules.PLACES)
    p.add_argument("--energy", choices=rules.ENERGIES)
    sub.add_parser("undo", parents=[common]).add_argument("event_id", type=int)
    return ap


# ---- helpers -------------------------------------------------------------------------------
def task_json(t, now: datetime) -> dict:
    out = {}
    for c in db.TASK_COLUMNS:
        v = getattr(t, c)
        out[c] = v.astimezone(ET).isoformat() if isinstance(v, datetime) else v
    out["score"] = round(rules.score(t, now), 4)
    out["urgent"] = rules.urgent(t, now)
    out["why"] = rules.explain(t, now)
    return out


def parse_due(text: str, now: datetime) -> datetime:
    try:
        dt = datetime.fromisoformat(text.strip())
        return dt.replace(tzinfo=ET) if dt.tzinfo is None else dt.astimezone(ET)
    except ValueError:
        pass
    dt = parse_when(text, now)
    if dt is None:
        raise Refused("bad_due", f"Couldn't read the date \"{text}\". Use ISO (2026-10-03T15:00) "
                                 "or a phrase like \"Friday 3pm\", \"tomorrow\", \"10/9\".")
    return dt


def open_service(path: Path, clock) -> TaskService:
    if not Path(path).exists():
        raise Refused("no_database", f"No database at {path}.")
    conn = db.connect(path)
    have = db.current_version(conn)
    latest = max(int(f.name.split("_", 1)[0]) for f in db.MIGRATIONS_DIR.glob("*.sql"))
    if have < latest:
        raise Refused("schema_old", f"The database is at version {have}, this code needs {latest}. "
                                    "Run: python -m dispatch migrate")
    return TaskService(conn, clock)


def _get_open(s: TaskService, tid: int):
    t = s.get(tid)
    if t is None:
        raise Refused("not_found", f"There is no task #{tid}.")
    return t


# ---- commands ------------------------------------------------------------------------------
def cmd_add(s: TaskService, a, now: datetime, guesser) -> dict:
    title = a.title.strip()
    if not title:
        raise Refused("empty_title", "The title is empty.")
    if a.area is not None and normalize(a.area) == UNSORTED and a.area.strip().lower() != UNSORTED:
        raise Refused("bad_area", f"Unknown area \"{a.area}\". Use one of: {', '.join(AREAS)}.")
    due = parse_due(a.due, now) if a.due else None
    if a.external_ref:
        existing = s.get_by_ref(a.external_ref)
        if existing is not None:                  # dedupe before spending any AI time
            return {"ok": True, "created": False, "task": task_json(existing, now),
                    "guess": {"model": None, "ms": 0, "guessed": []}, "promoted": []}
    g = qwen.Guess() if a.no_guess else guesser(title, a.notes)
    explicit = {"area": a.area, "payoff": a.payoff, "likelihood": a.likelihood,
                "estimate_minutes": a.estimate, "place": a.place, "energy": a.energy}
    guessed = [f for f in g.guessed if explicit.get(f) is None]

    def pick(field, mine):
        return mine if mine is not None else (getattr(g, field) if field in guessed else None)

    task, created = s.add_unique(
        title=title, area=pick("area", a.area), due_at=due, source=a.source, notes=a.notes,
        estimate_minutes=pick("estimate_minutes", a.estimate), payoff=pick("payoff", a.payoff),
        likelihood=pick("likelihood", a.likelihood), status="parked" if a.park else None,
        guessed=guessed, external_ref=a.external_ref, actor="cli", info=g.info(),
        place=pick("place", a.place), energy=pick("energy", a.energy),
        fill_by_rank=True)            # a burst of adds (e.g. the Gmail sweep) fills slots by rank
    task = s.get(task.id)
    promoted = []
    if created:     # OTHER items this add moved into the active three (its own status is in "task")
        rows = s.conn.execute("SELECT DISTINCT task_id FROM events WHERE kind='promoted' AND "
                              "cause_id=? AND task_id != ? ORDER BY id",
                              (s.last_event_id, task.id)).fetchall()
        promoted = [task_json(s.get(r[0]), now) for r in rows]
    return {"ok": True, "created": created, "task": task_json(task, now),
            "guess": {"model": g.model, "ms": g.ms, "guessed": guessed}, "promoted": promoted}


def cmd_list(s: TaskService, a, now: datetime) -> dict:
    statuses = [x.strip() for x in a.status.split(",") if x.strip()]
    bad = [x for x in statuses if x not in STATUSES]
    if bad or not statuses:
        raise Refused("bad_status", f"Unknown status {bad}. Use: {', '.join(STATUSES)}.")
    tasks = s.by_status(*statuses)
    if a.area:
        tasks = [t for t in tasks if t.area == normalize(a.area)]
    if a.due_before:
        limit = parse_due(a.due_before, now)
        tasks = [t for t in tasks if t.due_at is not None and t.due_at < limit]
    tasks = rules.rank(tasks, now)
    if a.limit is not None:
        tasks = tasks[:max(a.limit, 0)]
    return {"ok": True, "count": len(tasks), "tasks": [task_json(t, now) for t in tasks]}


def run_command(s: TaskService, a, now: datetime, guesser) -> dict:
    if a.cmd == "add":
        return cmd_add(s, a, now, guesser)
    if a.cmd == "list":
        return cmd_list(s, a, now)
    if a.cmd == "undo":
        try:
            res = s.undo(a.event_id, actor="cli")
        except UndoRefused as exc:
            raise Refused("undo_refused", str(exc))
        return {"ok": True, "reversed": res.reversed_ids, "message": res.text, "event_id": res.event_id}
    t = _get_open(s, a.id)
    if a.cmd == "get":
        evs = [{"id": r["id"], "kind": r["kind"], "at": db.from_db(r["at"]).isoformat(),
                "actor": r["actor"], "detail": db.detail_of(r)} for r in db.events_for(s.conn, t.id)]
        return {"ok": True, "task": task_json(t, now), "events": evs}
    try:
        if a.cmd in ("done", "kill"):
            t, promoted = (s.mark_done if a.cmd == "done" else s.kill)(t.id, actor="cli")
        elif a.cmd == "reschedule":
            t, promoted = s.reschedule(t.id, parse_due(a.due, now), actor="cli")
        elif a.cmd in ("estimate", "set"):
            fields = ({"estimate_minutes": a.minutes} if a.cmd == "estimate" else
                      {k: v for k, v in (("payoff", a.payoff), ("likelihood", a.likelihood),
                                         ("area", a.area), ("estimate_minutes", a.estimate),
                                         ("place", a.place), ("energy", a.energy))
                       if v is not None})
            if not fields:
                raise Refused("nothing_to_set", "Give at least one of --payoff --likelihood "
                                                "--area --estimate --place --energy.")
            if "area" in fields and normalize(fields["area"]) == UNSORTED:
                raise Refused("bad_area", f"Unknown area \"{fields['area']}\". Use one of: {', '.join(AREAS)}.")
            t = s.set_fields(t.id, actor="cli", **fields)
            return {"ok": True, "task": task_json(t, now), "event_id": s.last_event_id}
    except NotOpen:
        raise Refused("not_open", f"#{t.id} is already {t.status}.")
    return {"ok": True, "task": task_json(t, now), "promoted": [task_json(p, now) for p in promoted],
            "event_id": s.last_event_id}


def _emit(obj: dict, out) -> None:
    out.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")
    out.flush()


def main(argv: list[str] | None = None, clock=None, guesser=None, out=None, settings=None) -> int:
    """Entry point. Returns the exit code; prints exactly one JSON object."""
    out = out or sys.stdout
    try:
        a = build_parser().parse_args(argv)
    except BadArgs as exc:
        _emit({"ok": False, "error": "bad_arguments", "message": str(exc)}, out)
        return 2
    clock = clock or SystemClock()
    settings = settings or load_settings()
    if guesser is None:
        def guesser(title, notes):
            return qwen.guess_fields(title, notes, settings.ollama_url)
    s = None
    try:
        s = open_service(a.db or settings.db_path, clock)
        result = run_command(s, a, clock.now(), guesser)
    except Refused as exc:
        _emit({"ok": False, "error": exc.code, "message": exc.message}, out)
        return 1
    except Exception as exc:  # noqa: BLE001 - still one JSON object, never a bare traceback
        print(f"dispatch cli: {type(exc).__name__}: {exc}", file=sys.stderr)
        _emit({"ok": False, "error": "internal", "message": f"{type(exc).__name__}: {exc}"}, out)
        return 1
    finally:
        if s is not None:
            s.conn.close()
    _emit(result, out)
    return 0
