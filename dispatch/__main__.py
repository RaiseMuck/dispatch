"""Command line.

    python -m dispatch migrate          create/upgrade dispatch.db
    python -m dispatch import-seed      load seed.json into dispatch.db (skips items already there)
    python -m dispatch run              start the Discord bot (needs .env)
    python -m dispatch --dry-run --simulate 2026-09-28T07:00:00-04:00 --hours 27
                                        fake clock + temp database + printed output; no Discord
                                        (--canvas-fixture tests/fixtures/canvas-sample.ics adds
                                        a Canvas feed to the simulation; --scenario sick-day
                                        --seed tests/fixtures/seed-sick-day.json plays a sick
                                        day: mode at 8:05, a swap, a minutes query, a did:)
    python -m dispatch cli <command> ...   operate the list without Discord (JSON; see cli.py)
    python -m dispatch stats --days 7      daily-three hit rate (the Phase 2 success test)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import tempfile
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import canvas, chat, db, qwen, rules
from .areas import weight
from .clock import ET, FakeClock, SystemClock, fmt_et
from .config import PROJECT_ROOT, load_settings
from .scheduler import PrintPoster, RunState, tick
from .service import LadderError, TaskService, describe

SEED_PATH = PROJECT_ROOT / "seed.json"


def import_seed(service: TaskService, path: Path = SEED_PATH) -> int:
    """Load seed.json. Soonest-due and heaviest items are added first so they get the 3 slots."""
    items = json.loads(Path(path).read_text(encoding="utf-8"))["items"]
    far = datetime(9999, 1, 1, tzinfo=ET)

    def due_of(it):
        return datetime.fromisoformat(it["due_at"]).astimezone(ET) if it.get("due_at") else None

    items.sort(key=lambda it: (due_of(it) or far, -weight(it["area"])))
    existing = {r[0] for r in service.conn.execute("SELECT title FROM tasks WHERE source='seed'")}
    added = 0
    for it in items:
        if it["title"] in existing:
            continue
        # A seed row keeps its explicit "status" (e.g. "parked"); without one, the 3-slot rule decides.
        # place / energy / estimate_minutes are optional (only the scenario seed has them)
        service.add(it["title"], area=it["area"], due_at=due_of(it), source="seed",
                    notes=it.get("notes"), status=it.get("status"), place=it.get("place"),
                    energy=it.get("energy"), estimate_minutes=it.get("estimate_minutes"))
        added += 1
    return added


def open_service(db_path: Path, clock) -> TaskService:
    conn = db.connect(db_path)
    db.migrate(conn, clock.now())
    return TaskService(conn, clock)


class Script:
    """Scripted taps for the dry run: +2h, then Tonight, then show the no-snooze menu."""

    def __init__(self, service: TaskService, out):
        self.s, self.out = service, out
        self.stage = 0
        self.target: int | None = None
        self.morning_ids: list[int] = []
        self.done_tapped = False

    def after_tick(self, now: datetime, sent) -> None:
        for msg in sent:
            if msg.kind == "morning" and not self.morning_ids:
                self.morning_ids = [t.id for t in msg.items]
            if msg.kind != "nag":
                continue
            ids = [t.id for t in msg.items]
            if self.stage == 0:
                self.target = ids[0]
                t = self.s.snooze(self.target, rules.PLUS2H)
                self.out(f"  >> Max taps [+2h] on #{t.id} -> comes back {fmt_et(t.next_nag_at)}"
                         f" (snooze {t.snooze_count}/2)")
                self.stage = 1
            elif self.stage == 1 and self.target in ids:
                t = self.s.snooze(self.target, rules.TONIGHT)
                self.out(f"  >> Max taps [Tonight] on #{t.id} -> comes back {fmt_et(t.next_nag_at)}"
                         f" (snooze {t.snooze_count}/2)")
                self.stage = 2
            elif self.stage == 2 and self.target in ids:
                t = self.s.get(self.target)
                opts = " / ".join(rules.LABELS[o] for o in rules.snooze_options(t.snooze_count))
                self.out(f"  >> THIRD time #{t.id} comes up. Menu offered: {opts}")
                try:
                    self.s.snooze(t.id, rules.PLUS2H)
                    self.out("  !! BUG: a third snooze was accepted")
                except LadderError as exc:
                    self.out(f"  >> Max tries [+2h] anyway -> refused ({exc})")
                self.out("  >> Max does not answer, so it keeps being re-listed every hour.")
                self.stage = 3
        if (not self.done_tapped and self.morning_ids and now.hour >= 13
                and now.date() == self._start_date):
            tid = next((i for i in reversed(self.morning_ids) if i != self.target), None)
            if tid is not None:
                t, promoted = self.s.mark_done(tid)
                extra = "".join(f"; promoted #{p.id} {p.title}" for p in promoted)
                self.out(f"  >> [{fmt_et(now)} ET] Max taps [Done] on #{t.id}{extra}")
            self.done_tapped = True

    _start_date = None


class SickDayScript:
    """The Phase 2.1 'sick day' scenario (plan 10.3), day 1 of the run:
    08:05 Max taps [Sick/home day]; 10:00 [Swap] -> [Too long] on the first of the three;
    10:05 [Swap] -> [Wrong energy] on the second; 11:30 "I've got 20 minutes"; 15:00 "did: set
    up Dispatch". The local AI is off (as in every dry run), so the chat parts go straight to
    the deterministic code the AI's answer feeds."""

    def __init__(self, service: TaskService, out):
        self.s, self.out = service, out

        async def inline(fn, *a):
            return fn(*a)
        self.router = chat.Router(service, ask=lambda prompt: None,
                                  guess=lambda title, notes: qwen.Guess(), run=inline)
        self.done: set[str] = set()
        self.mode_three: list[int] = []
        self.mode_event: int | None = None
        self._start_date = None

    def _at(self, now: datetime, key: str, hour: int, minute: int) -> bool:
        if key in self.done or now.date() != self._start_date:
            return False
        if (now.hour, now.minute) >= (hour, minute):
            self.done.add(key)
            return True
        return False

    def _print(self, now: datetime, what: str, r) -> None:
        self.out(f"  >> [{fmt_et(now)} ET] {what}")
        self.out("     " + r.text.replace("\n", "\n     "))
        btns = [label for label, _ in r.buttons] + (["Undo"] if r.undo_event_id else [])
        if btns:
            self.out("     buttons: " + " ".join(f"[{b}]" for b in btns))

    def after_tick(self, now: datetime, sent) -> None:
        if self._at(now, "mode", 8, 5):
            res = self.s.set_mode(rules.SICK, actor="button", source="button")
            self.mode_three = [t.id for t in res.active]
            self.mode_event = res.event_id
            self._print(now, "Max taps [Sick/home day]",
                        chat.Reply(res.text, res.event_id, self.mode_three, "set_mode"))
        if self._at(now, "swap", 10, 0):
            first = self.s.morning_items(now)[0]
            r = asyncio.run(self.router.swap_reply(first.id, "long", "button"))
            self._print(now, f"Max taps [Swap #{first.id}] -> [Too long]", r)
        if self._at(now, "swap2", 10, 5):
            # the sick-day fill is three ~15-min items, so "Too long" rightly finds nothing
            # shorter; a second tap shows a swap that goes through (and its promotion)
            second = self.s.morning_items(now)[1]
            r = asyncio.run(self.router.swap_reply(second.id, "energy", "button"))
            self._print(now, f"Max taps [Swap #{second.id}] -> [Wrong energy]", r)
        if self._at(now, "minutes", 11, 30):
            r = self.router._minutes({"action": "minutes", "minutes": 20}, "I've got 20 minutes")
            self._print(now, 'Max: "I\'ve got 20 minutes"', r)
        if self._at(now, "did", 15, 0):
            r = asyncio.run(self.router.handle("did: set up Dispatch"))
            self._print(now, 'Max: "did: set up Dispatch"', r)


def silence_gaps(sent, start: datetime, end: datetime, service: TaskService) -> dict[int, timedelta]:
    """Per task: the longest stretch it went without appearing in any message."""
    seen: dict[int, list[datetime]] = {}
    for when, msg in sent:
        for t in msg.shown():
            seen.setdefault(t.id, []).append(when)
    out = {}
    for t in service.by_status("active", "queued", "parked", "done", "killed"):
        stop = min(end, t.closed_at) if t.closed_at else end
        points = [start] + sorted(seen.get(t.id, [])) + [stop]
        out[t.id] = max(b - a for a, b in zip(points, points[1:]))
    return out


def dry_run(start_iso: str, hours: float, seed: Path, out=print, summary_only: bool = False,
            canvas_fixture: Path | None = None, canvas_fixture_later: Path | None = None,
            scenario: str | None = None, on_done=None) -> int:
    start = datetime.fromisoformat(start_iso)
    if start.tzinfo is None:
        start = start.replace(tzinfo=ET)
    say = out
    detail = (lambda *_: None) if summary_only else out    # --summary-only hides the transcript
    clock = FakeClock(start)
    tmp = Path(tempfile.mkdtemp(prefix="dispatch-dryrun-"))
    service = open_service(tmp / "dispatch.db", clock)
    n = import_seed(service, seed)
    say(f"DRY RUN - fake clock from {fmt_et(clock.now())} ET for {hours}h, temp db {tmp}")
    say(f"Loaded {n} seed items: "
        + ", ".join(f"{s}={len(service.by_status(s))}" for s in ("active", "queued", "parked")))
    for t in service.morning_items():
        detail(f"  active: {describe(t)}")
        detail(f"      why: {rules.explain(t, clock.now())}")
    ics = canvas_fixture.read_text(encoding="utf-8") if canvas_fixture else None
    if ics is not None:
        say(f"Canvas fixture: {canvas_fixture.name} (polled every 2 h through the same code; "
            "AI guesses off in the dry run)")
    detail("-" * 78)
    poster = PrintPoster(clock, detail)
    state = RunState(heartbeat_path=tmp / "heartbeat.json")
    script = SickDayScript(service, detail) if scenario == "sick-day" else Script(service, detail)
    if scenario == "sick-day":
        say("Scenario: sick day (8:05 Sick/home day, 10:00 Swap/Too long, 10:05 Swap/Wrong "
            "energy, 11:30 \"I've got 20 minutes\", 15:00 \"did: set up Dispatch\")")
    script._start_date = clock.now().date()
    end = start + timedelta(hours=hours)
    later = canvas_fixture_later.read_text(encoding="utf-8") if canvas_fixture_later else None
    switch_at = start + timedelta(hours=12)       # the "professor moved a date" moment
    while clock.now() <= end:
        if ics is not None:
            feed = later if (later is not None and clock.now() >= switch_at) else ics
            res = canvas.sync(service, clock.now(), lambda: feed)
            if res is not None and (res.added or res.moved or res.error):
                detail(f"[{fmt_et(clock.now())} ET] {res.summary()}")
                for t in res.added:
                    detail(f"      + {describe(t)}")
            canvas.flush_note(service, poster, clock.now())
            canvas.flush_move_notes(service, poster, clock.now())
        sent = tick(service, poster, clock, state)
        script.after_tick(clock.now(), sent)
        clock.advance(timedelta(minutes=1))
    say("-" * 78)
    quiet = [w for w, _ in poster.sent if rules.in_quiet_hours(w)]
    say(f"Messages sent: {len(poster.sent)}; during quiet hours 00:00-07:59 ET: {len(quiet)}")
    per_day: dict[str, dict[str, int]] = {}
    for w, m in poster.sent:
        day = per_day.setdefault(f"{w:%a %Y-%m-%d}", {})
        day[m.kind] = day.get(m.kind, 0) + 1
    for day, kinds in per_day.items():
        say(f"  {day}: {sum(kinds.values())} messages ("
            + ", ".join(f"{k}={v}" for k, v in sorted(kinds.items())) + ")")
    reminders = [w for w, m in poster.sent if m.kind != "weekly"]
    gaps = [(b - a) for a, b in zip(reminders, reminders[1:])]
    if gaps:
        say(f"Shortest gap between reminder messages (weekly review excluded): {min(gaps)}")
    silence = silence_gaps(poster.sent, start, end, service)
    say("Longest silence per item (start of run -> first mention, between mentions, last -> end):")
    for tid, gap in sorted(silence.items(), key=lambda kv: -kv[1]):
        t = service.get(tid)
        say(f"  #{tid:<3} {str(gap):>16}  {t.status:<7} {t.title[:60]}")
    hb = json.loads((tmp / "heartbeat.json").read_text(encoding="utf-8"))
    say(f"heartbeat.json: {hb}")
    counts = service.conn.execute("SELECT kind, COUNT(*) FROM events GROUP BY kind ORDER BY kind")
    say("events: " + ", ".join(f"{k}={c}" for k, c in counts))
    if on_done is not None:
        on_done(service, script, poster)
    return 0


def setup_logging(to_file: bool) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if to_file:
        (PROJECT_ROOT / "logs").mkdir(exist_ok=True)
        handlers.append(RotatingFileHandler(PROJECT_ROOT / "logs" / "dispatch.log",
                                            maxBytes=1_000_000, backupCount=3, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "voice":
        from . import voice
        return voice.main(argv[1:])
    if argv and argv[0] in ("cli", "stats"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        if argv[0] == "cli":
            from . import cli
            return cli.main(argv[1:])
        from . import stats
        return stats.main(argv[1:])
    ap = argparse.ArgumentParser(prog="python -m dispatch")
    ap.add_argument("command", nargs="?", choices=["run", "import-seed", "migrate"])
    ap.add_argument("--dry-run", action="store_true", help="simulate with a fake clock, no Discord")
    ap.add_argument("--simulate", help="start time for --dry-run, ISO format")
    ap.add_argument("--hours", type=float, default=27.0)
    ap.add_argument("--db", type=Path, help="database file (default: dispatch.db next to this folder)")
    ap.add_argument("--seed", type=Path, default=SEED_PATH)
    ap.add_argument("--summary-only", action="store_true",
                    help="with --dry-run: print only the counts, not every message")
    ap.add_argument("--canvas-fixture", type=Path,
                    help="with --dry-run: feed this .ics file through the Canvas sync")
    ap.add_argument("--canvas-fixture-later", type=Path,
                    help="with --canvas-fixture: switch to this .ics 12 h into the run (date moves)")
    ap.add_argument("--scenario", choices=["sick-day"],
                    help="with --dry-run: play a scripted day (use --seed "
                         "tests/fixtures/seed-sick-day.json)")
    a = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    if a.dry_run:
        return dry_run(a.simulate or SystemClock().now().isoformat(), a.hours, a.seed,
                       summary_only=a.summary_only, canvas_fixture=a.canvas_fixture,
                       canvas_fixture_later=a.canvas_fixture_later, scenario=a.scenario)

    settings = load_settings()
    db_path = a.db or settings.db_path
    clock = SystemClock()
    if a.command == "migrate":
        conn = db.connect(db_path)
        applied = db.migrate(conn, clock.now())
        print(f"database {db_path}: applied {applied or 'nothing (already current)'}")
        return 0
    if a.command == "import-seed":
        service = open_service(db_path, clock)
        n = import_seed(service, a.seed)
        print(f"imported {n} new seed items into {db_path}")
        for t in service.morning_items():
            print(f"  active: {describe(t)}")
        return 0
    if a.command == "run":
        problems = settings.problems()
        if problems:
            print("Can't start yet:\n  - " + "\n  - ".join(problems))
            print("Fill these in the .env file (see SETUP.md), then run again.")
            return 2
        setup_logging(to_file=True)
        from .bot import run_bot   # imported here so dry-run/tests never need discord.py
        run_bot(settings, open_service(db_path, clock), clock)
        return 0
    ap.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
