"""Phase 2.1 place/energy: stored on add, corrected by Max (needs_set, undoable), guessed by the
local AI only where empty (never blocking an Undo of Max's own change)."""
from datetime import datetime

import pytest

from dispatch import db, rules
from dispatch.clock import ET

NOW = datetime(2026, 10, 5, 10, 0, tzinfo=ET)


def rows(service):
    return [tuple(r) for r in service.conn.execute("SELECT * FROM tasks ORDER BY id")]


def test_add_stores_valid_needs_only(service, clock):
    clock.set(NOW)
    t = service.add("call Sam", place="phone", energy="light", guessed=["place", "energy"])
    assert (t.place, t.energy, t.guessed) == ("phone", "light", ["place", "energy"])
    t = service.add("x", place="couch", energy="tired")
    assert (t.place, t.energy) == (None, None)


def test_correction_is_needs_set_and_undoable(service, clock):
    clock.set(NOW)
    t = service.add("Strimer return", place="home", energy="light", guessed=["place", "energy"])
    before = rows(service)
    t = service.set_fields(t.id, actor="chat", place="out")
    assert t.place == "out" and t.guessed == ["energy"]
    ev = db.get_event(service.conn, service.last_event_id)
    assert ev["kind"] == "needs_set" and db.detail_of(ev)["after"] == {"place": "out"}
    service.undo(ev["id"], actor="chat")
    assert rows(service) == before
    with pytest.raises(ValueError):
        service.set_fields(t.id, actor="chat", place="car")


def test_guess_fills_only_empty_columns(service, clock):
    clock.set(NOW)
    a = service.add("a", place="out")
    t = service.set_needs_guess(a.id, "home", "focused")
    assert (t.place, t.energy) == ("out", "focused") and t.guessed == ["energy"]
    ev = db.events_for(service.conn, a.id)[-1]
    assert ev["kind"] == "needs_set" and ev["actor"] == "qwen"
    assert service.set_needs_guess(a.id, "phone", "light").place == "out"   # nothing left to fill


def test_a_background_guess_does_not_block_undo(service, clock):
    clock.set(NOW)
    a = service.add("a")
    service.snooze(a.id, rules.PLUS2H, actor="button")
    eid = service.last_event_id
    service.set_needs_guess(a.id, "home", "light")
    assert service.latest_undoable() == eid
    service.undo(eid, actor="chat")                          # not refused
    assert service.get(a.id).snooze_count == 0 and service.get(a.id).place == "home"


# ---- review r1 should-fix 3: small backfill batches, failure leaves items unknown -------------
def test_backfill_works_in_batches_of_8_and_retries_after_a_failure(service, clock):
    import asyncio
    from datetime import timedelta

    import fakes
    from dispatch import qwen
    clock.set(NOW)
    ids = [service.add(f"item {i}").id for i in range(20)]
    calls, fail = [], [True]

    def needs(items):
        calls.append([i for i, _ in items])
        if fail[0]:
            return {}                                          # AI down / timed out
        return {i: ("home", "light") for i, _ in items[:-1]}   # skips the last one each time

    r = fakes.router(service, needs=needs)
    assert asyncio.run(r.backfill()) == 0
    assert len(calls[0]) == qwen.NEEDS_BATCH == 8
    assert all(service.get(i).place is None for i in ids)      # nothing written on failure
    assert asyncio.run(r.backfill()) == 0 and len(calls) == 1  # resting (5 minutes)
    clock.advance(timedelta(minutes=5))
    fail[0] = False
    assert asyncio.run(r.backfill()) == 7                       # retried on a later tick
    for _ in range(12):
        asyncio.run(r.backfill())
    unknown = [i for i in ids if service.get(i).place is None]
    assert unknown and len(calls) < 20                         # skipped ones asked at most 3x
    assert qwen.NEEDS_TIMEOUTS[0] > qwen.GUESS_TIMEOUTS[0]
