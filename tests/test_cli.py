"""`python -m dispatch cli ...`: JSON shapes, exit codes, dedupe on external_ref, AI guesses vs
explicit flags, and running at the same time as the bot's minute loop."""
import io
import json
import sqlite3
import threading
import time as _time
from datetime import datetime, timedelta

import pytest

from dispatch import cli, db, qwen
from dispatch.clock import ET, FakeClock
from dispatch.config import Settings
from dispatch.scheduler import PrintPoster, RunState, tick
from dispatch.service import TaskService

NOW = datetime(2026, 10, 5, 9, 0, tzinfo=ET)
G = qwen.Guess(area="Coreflow/CLARA", payoff=4, likelihood=0.8, estimate_minutes=25,
               guessed=["area", "payoff", "likelihood", "estimate_minutes"], model="qwen3.5:27b", ms=900)
TASK_KEYS = set(db.TASK_COLUMNS) | {"score", "urgent", "why"}


@pytest.fixture
def dbfile(tmp_path):
    p = tmp_path / "cli.db"
    conn = db.connect(p)
    db.migrate(conn, NOW)
    conn.close()
    return p


def run(dbfile, *args, guesser=lambda t, n: G, clock=None):
    out = io.StringIO()
    code = cli.main([*args, "--db", str(dbfile), "--json"], clock=clock or FakeClock(NOW),
                    guesser=guesser, out=out, settings=Settings())   # never reads a real .env
    text = out.getvalue()
    assert text.count("\n") == 1, text                     # exactly one JSON object
    return code, json.loads(text)


def test_add_shape_guesses_and_iso_times(dbfile):
    code, r = run(dbfile, "add", "Reply to Sam about the pilot", "--due", "Friday 3pm",
                  "--source", "gmail", "--external-ref", "gmail:18c2f")
    assert code == 0 and set(r) == {"ok", "created", "task", "guess", "promoted"}
    t = r["task"]
    assert set(t) == TASK_KEYS and r["created"] is True and r["promoted"] == []
    assert t["due_at"] == "2026-10-09T15:00:00-04:00" and t["source"] == "gmail"
    assert t["external_ref"] == "gmail:18c2f" and t["area"] == "Coreflow/CLARA" and t["status"] == "active"
    assert t["guessed"] == ["area", "payoff", "likelihood", "estimate_minutes"]
    assert r["guess"] == {"model": "qwen3.5:27b", "ms": 900,
                          "guessed": ["area", "payoff", "likelihood", "estimate_minutes"]}
    assert t["score"] > 0 and t["urgent"] is False and t["why"].startswith("score")


def test_due_iso_naive_means_eastern_and_offset_is_respected(dbfile):
    _, r = run(dbfile, "add", "a", "--due", "2026-10-07T13:30", "--no-guess")
    assert r["task"]["due_at"] == "2026-10-07T13:30:00-04:00"
    _, r = run(dbfile, "add", "b", "--due", "2026-10-07T17:30:00+00:00", "--no-guess")
    assert r["task"]["due_at"] == "2026-10-07T13:30:00-04:00"
    code, r = run(dbfile, "add", "c", "--due", "whenever")
    assert code == 1 and r["error"] == "bad_due"


def test_explicit_flags_win_and_leave_guessed(dbfile):
    _, r = run(dbfile, "add", "x", "--payoff", "2", "--area", "School", "--estimate", "90")
    t = r["task"]
    assert (t["payoff"], t["area"], t["estimate_minutes"], t["likelihood"]) == (2, "School", 90, 0.8)
    assert t["guessed"] == ["likelihood"]


def test_no_guess_never_calls_the_ai(dbfile):
    def boom(*_):
        raise AssertionError("AI called")
    _, r = run(dbfile, "add", "quiet one", "--no-guess", "--park", guesser=boom)
    assert r["task"]["status"] == "parked" and r["task"]["guessed"] == [] and r["guess"]["model"] is None


def test_dedupe_on_external_ref_even_after_done(dbfile):
    _, first = run(dbfile, "add", "Pay the parking ticket", "--external-ref", "gmail:abc")
    calls = []
    _, again = run(dbfile, "add", "Pay the parking ticket (again)", "--external-ref", "gmail:abc",
                   guesser=lambda t, n: calls.append(t) or G)
    assert again["created"] is False and again["task"]["id"] == first["task"]["id"] and calls == []
    run(dbfile, "done", str(first["task"]["id"]))
    _, third = run(dbfile, "add", "Pay the parking ticket", "--external-ref", "gmail:abc")
    assert third["created"] is False and third["task"]["status"] == "done"   # not resurrected
    _, same_title = run(dbfile, "add", "Pay the parking ticket")               # no ref: no dedupe
    assert same_title["created"] is True and same_title["task"]["id"] != first["task"]["id"]


def test_dedupe_race_is_caught_by_the_unique_index(dbfile, monkeypatch):
    _, first = run(dbfile, "add", "mail 1", "--external-ref", "gmail:race", "--no-guess")
    real = TaskService.get_by_ref
    misses = {"n": 0}

    def flaky(self, ref):                 # both pre-checks miss, as if another process raced us
        if misses["n"] < 2:
            misses["n"] += 1
            return None
        return real(self, ref)

    monkeypatch.setattr(TaskService, "get_by_ref", flaky)
    code, r = run(dbfile, "add", "mail 1", "--external-ref", "gmail:race", "--no-guess")
    assert code == 0 and r["created"] is False and r["task"]["id"] == first["task"]["id"]


def test_other_commands_shapes(dbfile):
    ids = [run(dbfile, "add", f"t{i}", "--no-guess", "--area", "School")[1]["task"]["id"] for i in range(4)]
    code, r = run(dbfile, "list")
    assert code == 0 and set(r) == {"ok", "count", "tasks"} and r["count"] == 4
    assert all(set(t) == TASK_KEYS for t in r["tasks"])
    _, r = run(dbfile, "list", "--status", "queued")
    assert [t["id"] for t in r["tasks"]] == [ids[3]]
    _, r = run(dbfile, "get", str(ids[0]))
    assert set(r) == {"ok", "task", "events"} and r["events"][0]["kind"] == "created"
    assert r["events"][0]["actor"] == "cli"
    code, r = run(dbfile, "done", str(ids[0]))
    assert code == 0 and set(r) == {"ok", "task", "promoted", "event_id"}
    assert r["task"]["status"] == "done" and [p["id"] for p in r["promoted"]] == [ids[3]]
    _, r = run(dbfile, "reschedule", str(ids[1]), "--due", "2026-10-20")
    assert r["task"]["due_at"] == "2026-10-20T00:00:00-04:00" and r["task"]["status"] == "queued"
    _, r = run(dbfile, "estimate", str(ids[2]), "--minutes", "45")
    assert set(r) == {"ok", "task", "event_id"} and r["task"]["estimate_minutes"] == 45
    _, r = run(dbfile, "set", str(ids[2]), "--payoff", "5", "--likelihood", "0.9", "--area", "Portfolio")
    assert (r["task"]["payoff"], r["task"]["likelihood"], r["task"]["area"]) == (5, 0.9, "Portfolio")
    code, r = run(dbfile, "undo", str(r["event_id"]))
    assert code == 0 and set(r) == {"ok", "reversed", "message", "event_id"}
    _, r = run(dbfile, "get", str(ids[2]))
    assert (r["task"]["payoff"], r["task"]["area"]) == (None, "School")


@pytest.mark.parametrize("args,code,error", [
    (["frobnicate"], 2, "bad_arguments"),
    (["add"], 2, "bad_arguments"),
    (["add", "x", "--payoff", "9"], 2, "bad_arguments"),
    (["add", "x", "--source", "email"], 2, "bad_arguments"),
    (["done", "999"], 1, "not_found"),
    (["undo", "999"], 1, "undo_refused"),
    (["set", "1"], 1, "nothing_to_set"),
    (["add", "x", "--area", "Banana"], 1, "bad_area"),
    (["list", "--status", "open"], 1, "bad_status"),
])
def test_errors_are_json_with_exit_codes(dbfile, args, code, error):
    run(dbfile, "add", "one", "--no-guess")
    got, r = run(dbfile, *args)
    assert (got, r["ok"], r["error"]) == (code, False, error) and r["message"]


def test_closed_task_and_old_schema_are_refused(dbfile, tmp_path):
    _, r = run(dbfile, "add", "one", "--no-guess")
    run(dbfile, "kill", str(r["task"]["id"]))
    code, r = run(dbfile, "done", str(r["task"]["id"]))
    assert (code, r["error"]) == (1, "not_open")
    old = tmp_path / "v2.db"
    conn = sqlite3.connect(old)
    for name in ("0001_init.sql", "0002_weekly_review.sql"):
        conn.executescript((db.MIGRATIONS_DIR / name).read_text(encoding="utf-8"))
    conn.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
    conn.execute("INSERT INTO schema_version VALUES (1, 'x'), (2, 'x')")
    conn.commit()
    conn.close()
    code, r = run(old, "list")
    assert (code, r["error"]) == (1, "schema_old")          # the CLI never migrates by itself
    assert sqlite3.connect(old).execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 2
    code, r = run(tmp_path / "missing.db", "list")
    assert (code, r["error"]) == (1, "no_database")


def test_cli_runs_while_the_bot_loop_is_ticking(dbfile):
    """A bot thread ticks every 10 ms (fake clock +1 min per tick) on its own connection while
    this thread runs 50 adds, 10 dones and 5 reschedules through the CLI on the same file."""
    errors, seen = [], {}
    stop = threading.Event()

    def bot():
        try:
            clock = FakeClock(NOW)
            conn = db.connect(dbfile)
            s = TaskService(conn, clock)
            poster = PrintPoster(clock, out=lambda *_: None)
            while not stop.is_set():
                tick(s, poster, clock, RunState())
                clock.advance(timedelta(minutes=1))
                _time.sleep(0.01)
            tick(s, poster, clock, RunState())            # one more tick after the CLI is done
            seen["titles"] = {t.title for t in s.by_status("active", "queued", "done")}
            seen["journal"] = conn.execute("PRAGMA journal_mode").fetchone()[0]
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    th = threading.Thread(target=bot)
    th.start()
    try:
        ids = []
        for i in range(50):
            code, r = run(dbfile, "add", f"cli task {i}", "--no-guess")
            assert code == 0, r
            ids.append(r["task"]["id"])
        for tid in ids[:10]:
            code, r = run(dbfile, "done", str(tid))
            assert code == 0, r
        for tid in ids[10:15]:
            code, r = run(dbfile, "reschedule", str(tid), "--due", "2026-10-30T10:00")
            assert code == 0, r
    finally:
        stop.set()
        th.join(timeout=30)
    assert errors == []
    assert seen["titles"] >= {f"cli task {i}" for i in range(50)}
    assert seen["journal"] == "wal"



def test_cli_add_fills_a_free_slot_by_rank(dbfile):
    """review r1 should-fix: a burst of CLI adds (the Gmail sweep) must not take free slots in
    arrival order - the best-ranked waiting item gets the slot."""
    conn = db.connect(dbfile)
    s = TaskService(conn, FakeClock(NOW))
    s.add("active a", area="School")
    s.add("active b", area="School")                                  # one slot free
    heavy = s.add("coreflow payoff 5", area="Coreflow/CLARA", payoff=5, status="queued")
    conn.close()
    _, r = run(dbfile, "add", "low value mail", "--area", "Home & life", "--payoff", "1", "--no-guess")
    assert r["task"]["status"] == "queued"                    # arrival order would make it active
    assert [p["id"] for p in r["promoted"]] == [heavy.id]     # review r2: the CLI reports it
    _, lst = run(dbfile, "list", "--status", "active")
    assert heavy.id in [t["id"] for t in lst["tasks"]] and lst["count"] == 3
    _, top = run(dbfile, "add", "urgent mail", "--due", "2026-10-05T17:00", "--no-guess")
    assert top["task"]["status"] == "queued"                  # no free slot: waits, never bumps
    assert top["promoted"] == []
    code, u = run(dbfile, "undo", str(run(dbfile, "get", str(r["task"]["id"]))[1]["events"][0]["id"]))
    assert code == 0                                          # undo of a rank-filled add works
