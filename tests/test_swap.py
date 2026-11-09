"""Phase 2.1 Swap: each reason's replacement rule, 'nothing fits', two swaps per item for life,
no same-day bounce-back, and Undo."""
from datetime import datetime, timedelta

import pytest

from dispatch import db, rules
from dispatch.clock import ET
from dispatch.service import NotActive

NOW = datetime(2026, 10, 5, 10, 0, tzinfo=ET)


def rows(service):
    return [tuple(r) for r in service.conn.execute("SELECT * FROM tasks ORDER BY id")]


@pytest.fixture
def s(service, clock):
    clock.set(NOW)
    ids = {}
    for key, place, energy, mins in [
            ("big", "home", "focused", 90), ("call", "phone", "light", 20),
            ("errand", "out", "light", 30),                       # the three
            ("q_short", "home", "light", 15), ("q_focus", "home", "focused", 60),
            ("q_out", "out", "light", 10), ("q_long", "home", "light", 120)]:
        ids[key] = service.add(key, area="Personal admin", place=place, energy=energy,
                               estimate_minutes=mins).id
    clock.advance(timedelta(minutes=1))
    return ids


def test_too_long_takes_something_strictly_shorter(service, s):
    res = service.swap(s["big"], "long", actor="button")
    assert res.replacement.id != s["q_long"]
    assert rules.needs(res.replacement)[2] < 90
    t = service.get(s["big"])
    assert t.status == "queued" and t.swap_count == 1 and t.snooze_count == 0
    p = service.conn.execute("SELECT * FROM events WHERE kind='promoted' AND cause_id=?",
                             (res.event_id,)).fetchone()
    assert p["task_id"] == res.replacement.id and db.detail_of(p)["via"] == "swap"


def test_cant_go_out_never_takes_an_out_item_and_marks_the_item(service, s):
    service.set_fields(s["errand"], actor="chat", energy="light")
    service.conn.execute("UPDATE tasks SET guessed='[\"place\", \"area\"]' WHERE id=?",
                         (s["errand"],))
    service.conn.commit()
    res = service.swap(s["errand"], "out", actor="button")
    assert rules.needs(res.replacement)[0] != "out"
    t = service.get(s["errand"])
    assert t.place == "out" and t.guessed == ["area"]


def test_wrong_energy_takes_the_other_level(service, s):
    res = service.swap(s["big"], "energy", actor="button")        # focused -> light
    assert rules.needs(res.replacement)[1] == "light"
    res = service.swap(s["call"], "energy", actor="button")       # light -> focused
    assert rules.needs(res.replacement)[1] == "focused"


def test_unknown_energy_means_find_something_light(service, clock):
    clock.set(NOW)
    x = service.add("x")                                           # energy unknown
    service.add("y")
    service.add("z")
    heavy = service.add("heavy", energy="focused", estimate_minutes=10, payoff=5)
    light = service.add("light", energy="light", estimate_minutes=30, payoff=1)
    res = service.swap(x.id, "energy", actor="button")
    assert res.replacement.id == light.id and heavy.status == "queued"


def test_nothing_fits_changes_nothing_and_is_logged(service, s):
    service.kill(s["q_short"])
    service.kill(s["q_out"])
    before = rows(service)
    res = service.swap(s["call"], "long", actor="button")            # nothing under 20 min
    assert res.nothing_fits and res.event_id is None and rows(service) == before
    ev = service.conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT 1").fetchone()
    assert ev["kind"] == "chat" and db.detail_of(ev)["action"] == "swap_nothing_fits"


def test_replacement_respects_the_day_mode(service, s):
    service.set_mode(rules.DayMode("custom", frozenset(rules.PLACES), 60, False), actor="chat")
    act = {t.id for t in service.by_status("active")}
    target = next(iter(act))
    res = service.swap(target, "waiting", actor="button")
    if res.replacement:
        assert rules.needs(res.replacement)[2] <= 60


def test_two_swaps_then_a_forced_decision(service, s, clock):
    r1 = service.swap(s["big"], "long", actor="button")
    assert r1.long_swaps == 1
    clock.advance(timedelta(days=1))
    service.start(s["big"], actor="button")                   # Max takes it back
    assert service.get(s["big"]).status == "active"
    r2 = service.swap(s["big"], "long", actor="button")
    assert r2.event_id and r2.long_swaps == 2
    clock.advance(timedelta(days=1))
    service.start(s["big"], actor="button")
    assert service.get(s["big"]).status == "active"
    n = service.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    r3 = service.swap(s["big"], "out", actor="button")
    assert r3.forced and r3.event_id is None
    assert service.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == n
    assert service.get(s["big"]).swap_count == 2


def test_swapped_item_cannot_bounce_back_the_same_day(service, s, clock):
    res = service.swap(s["big"], "long", actor="button")
    for t in service.by_status("queued"):
        if t.id != s["big"]:
            service.kill(t.id)                                # big is the only queued item
    _, promoted = service.kill(res.replacement.id)
    assert promoted == [] and service.get(s["big"]).status == "queued"   # not today
    clock.set(NOW.replace(hour=0, minute=1) + timedelta(days=1))
    _, promoted = service.kill(s["call"])
    assert [p.id for p in promoted] == [s["big"]]            # the next day it may come back


def test_undo_restores_swap_count_and_demotes_the_untouched_replacement(service, s):
    before = rows(service)
    res = service.swap(s["big"], "long", actor="button")
    u = service.undo(res.event_id, actor="button")
    assert rows(service) == before
    assert f"#{res.replacement.id} goes back to the queue" in u.text


def test_swap_needs_an_active_item(service, s):
    with pytest.raises(NotActive):
        service.swap(s["q_short"], "long", actor="chat")


def test_waiting_swap_leaves_the_slot_to_rank(service, s):
    res = service.swap(s["call"], "waiting", actor="button")
    t = service.get(s["call"])
    assert t.status == "waiting" and t.waiting_until == NOW + timedelta(days=3, minutes=1)
    assert t.swap_count == 1 and res.replacement is not None
    assert service.active_count() == 3
