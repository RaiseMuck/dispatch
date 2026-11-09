"""Phase 2.1 CLI: --place / --energy on add and set, the waiting status in list, new columns."""
from datetime import timedelta

from dispatch import db, qwen
from dispatch.clock import FakeClock
from dispatch.service import TaskService

from test_cli import NOW, dbfile, run  # noqa: F401 - the fixture and helper

G6 = qwen.Guess(area="Personal admin", payoff=3, likelihood=0.8, estimate_minutes=30,
                place="out", energy="light",
                guessed=["area", "payoff", "likelihood", "estimate_minutes", "place", "energy"])


def test_add_takes_guessed_needs_and_flags_win(dbfile):  # noqa: F811
    code, r = run(dbfile, "add", "Return the Strimer", guesser=lambda t, n: G6)
    t = r["task"]
    assert code == 0 and (t["place"], t["energy"]) == ("out", "light")
    assert set(t["guessed"]) >= {"place", "energy"}
    assert {"swap_count", "waiting_until", "parent_id"} <= set(t)
    code, r = run(dbfile, "add", "Call the broker", "--place", "phone", "--energy", "focused",
                  guesser=lambda t, n: G6)
    t = r["task"]
    assert (t["place"], t["energy"]) == ("phone", "focused")
    assert "place" not in t["guessed"] and "energy" not in t["guessed"]


def test_set_place_energy_and_bad_values(dbfile):  # noqa: F811
    run(dbfile, "add", "x", "--no-guess")
    code, r = run(dbfile, "set", "1", "--place", "home", "--energy", "light")
    assert code == 0 and (r["task"]["place"], r["task"]["energy"]) == ("home", "light")
    conn = db.connect(dbfile)
    assert db.get_event(conn, r["event_id"])["kind"] == "needs_set"
    conn.close()
    code, r = run(dbfile, "set", "1", "--place", "car")
    assert code == 2 and r["error"] == "bad_arguments"


def test_list_waiting(dbfile):  # noqa: F811
    run(dbfile, "add", "wait for Sam", "--no-guess")
    conn = db.connect(dbfile)
    s = TaskService(conn, FakeClock(NOW))
    s.set_waiting(1, NOW + timedelta(days=3), actor="chat")
    conn.close()
    code, r = run(dbfile, "list", "--status", "waiting")
    assert code == 0 and r["count"] == 1 and r["tasks"][0]["status"] == "waiting"
    assert r["tasks"][0]["waiting_until"].startswith("2026-10-08T09:00")
    code, r = run(dbfile, "list")                              # default: active,queued
    assert r["count"] == 0
