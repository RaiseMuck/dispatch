"""Phase 2.1 day modes: set, re-pick the three, no snooze used, deadlines kept in view,
midnight expiry, the next morning's re-pick, and Undo of all of it."""
import json
from datetime import datetime, timedelta

import pytest

from dispatch import db, rules
from dispatch.clock import ET
from dispatch.service import mode_key

NOW = datetime(2026, 10, 5, 8, 5, tzinfo=ET)          # Monday


def rows(service):
    return [tuple(r) for r in service.conn.execute("SELECT * FROM tasks ORDER BY id")]


def events(service, kind):
    return service.conn.execute("SELECT * FROM events WHERE kind=? ORDER BY id", (kind,)).fetchall()


@pytest.fixture
def day(service, clock):
    """3 active that don't fit a sick day + 4 queued (3 home, 1 out)."""
    clock.set(NOW - timedelta(minutes=5))
    add = service.add
    a = add("Strimer return", area="Personal admin", place="out", energy="light",
            estimate_minutes=45, due_at=NOW - timedelta(days=2))
    b = add("Keystone sessions", area="Personal admin", place="home", energy="focused",
            estimate_minutes=120)
    c = add("Call Sam", area="Personal admin", place="phone", energy="light", estimate_minutes=20)
    q1 = add("List assets", area="Personal admin", place="home", energy="light", estimate_minutes=20)
    q2 = add("Investing amount", area="Personal admin", place="home", energy="light",
             estimate_minutes=15)
    q3 = add("Bike money", area="Personal admin", place="home", energy="focused",
             estimate_minutes=40)
    q4 = add("Electroculture", area="Personal admin", place="out", energy="light",
             estimate_minutes=90)
    service.snooze(a.id, rules.PLUS2H, actor="button")
    clock.set(NOW)
    return {k: v.id for k, v in dict(a=a, b=b, c=c, q1=q1, q2=q2, q3=q3, q4=q4).items()}


def test_sick_day_repicks_the_three_without_using_snoozes(service, day):
    res = service.set_mode(rules.SICK, actor="button", source="button")
    assert res.event_id is not None and not res.same
    active = service.by_status("active")
    assert {t.id for t in active} == {day["q1"], day["q2"], day["q3"]}
    assert all(rules.fits(t, rules.SICK) for t in active)
    # light ones fill first, focused only because nothing else light fits
    assert [t.id for t in res.promoted][-1] == day["q3"]
    a = service.get(day["a"])
    assert a.status == "queued" and a.snooze_count == 1 and a.swap_count == 0
    assert a in service.due_batch()                       # overdue deadline keeps showing
    assert service.get(day["b"]).next_nag_at is None       # undated: waits quietly
    ev = events(service, "mode_set")[-1]
    d = db.detail_of(ev)
    assert ev["actor"] == "button" and d["mode"] == "sick" and d["before"] == {"meta": None}
    assert {x["id"] for x in d["side"]} == {day["a"], day["b"], day["c"]}
    rec = service.mode_record()
    assert rec["mode"] == "sick" and rec["event_id"] == ev["id"]
    for p in events(service, "promoted")[-3:]:
        assert p["cause_id"] == ev["id"] and db.detail_of(p)["via"] == "mode"
    assert "Sick/home day: home only, light stuff first, items <= 45 min." in res.text
    assert "Set aside for today:" in res.text and "needs going out" in res.text
    assert "it stays in the hourly message" in res.text   # the overdue Strimer return
    assert service.current_mode() == rules.SICK


def test_same_mode_twice_is_a_no_op(service, day):
    service.set_mode(rules.SICK, actor="chat")
    n = len(events(service, "mode_set"))
    res = service.set_mode(rules.SICK, actor="chat")
    assert res.same and res.event_id is None and len(events(service, "mode_set")) == n
    assert "Already set" in res.text
    res = service.set_mode(rules.NORMAL, actor="chat")      # normal after sick: stored
    assert res.event_id is not None and res.demoted == []
    assert service.mode_record()["mode"] == "normal" and service.current_mode() is None


def test_normal_with_no_mode_set_changes_nothing(service, day):
    before = rows(service)
    res = service.set_mode(rules.NORMAL, actor="chat")
    assert res.same and rows(service) == before and not events(service, "mode_set")


def test_total_minutes_budget_stops_the_fill(service, day):
    mode = rules.DayMode("custom", frozenset({"home"}), 45, True, 40)
    service.set_mode(mode, actor="chat")
    assert {t.id for t in service.by_status("active")} == {day["q1"], day["q2"]}   # 35 of 40 min


def test_nothing_fits_clears_the_three_and_says_so(service, clock):
    clock.set(NOW)
    for i in range(4):
        service.add(f"errand {i}", place="out", estimate_minutes=60)
    res = service.set_mode(rules.SICK, actor="chat")
    assert service.active_count() == 0
    assert "Nothing on the list fits a sick day - your three are cleared for today." in res.text


def test_mode_expires_at_midnight(service, day, clock):
    service.set_mode(rules.SICK, actor="chat")
    clock.set(NOW.replace(hour=23, minute=59))
    assert service.current_mode() == rules.SICK
    clock.set(NOW.replace(hour=0, minute=1) + timedelta(days=1))
    assert service.current_mode() is None


def test_undo_of_a_mode_restores_rows_and_meta(service, day):
    before = rows(service)
    res = service.set_mode(rules.SICK, actor="chat")
    u = service.undo(res.event_id, actor="chat")
    assert rows(service) == before
    assert service.meta_get(mode_key(NOW)) is None
    assert "Undid the day mode - today is back to no mode." in u.text
    undone = {r["id"] for r in service.conn.execute(
        "SELECT id FROM events WHERE undone_by=?", (u.event_id,))}
    assert res.event_id in undone and len(undone) == 4     # the mode + its 3 promotions


def test_undo_of_an_older_mode_is_refused_after_a_newer_one(service, day):
    from dispatch.service import UndoRefused
    first = service.set_mode(rules.SICK, actor="chat").event_id
    service.set_mode(rules.SLAMMED, actor="chat")
    with pytest.raises(UndoRefused, match="changed again"):
        service.undo(first)


def test_a_failure_mid_way_leaves_nothing_behind(service, day, monkeypatch):
    before = rows(service)

    def boom(*a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr(db, "meta_set", boom)
    with pytest.raises(RuntimeError):
        service.set_mode(rules.SICK, actor="chat")
    assert rows(service) == before and not events(service, "mode_set")
    assert not service.conn.in_transaction


def test_next_morning_repick_swaps_untouched_fillers_only(service, day, clock):
    service.set_mode(rules.SICK, actor="button")
    clock.advance(timedelta(minutes=30))
    service.do_now(day["q2"], actor="button")              # Max touched this one: it stays
    clock.set(NOW.replace(hour=8, minute=0) + timedelta(days=1))
    before = rows(service)
    pairs = service.morning_repick()
    assert pairs, "an overdue deadline beats a sick-day filler"
    assert all(o.id != day["q2"] for o, _ in pairs)
    assert service.get(day["q2"]).status == "active"
    assert service.get(day["a"]).status == "active"       # the overdue one is back
    assert service.active_count() == 3
    ev = events(service, "repick")[-1]
    assert ev["actor"] == "scheduler" and ev["task_id"] is None
    assert service.latest_undoable() == ev["id"]           # "undo that" reaches it
    assert service.morning_repick() == []                   # once only
    u = service.undo(ev["id"], actor="chat")
    assert rows(service) == before and "yesterday's three are back" in u.text


def test_no_repick_without_a_mode_yesterday_or_with_one_today(service, day, clock):
    clock.set(NOW.replace(hour=8, minute=0) + timedelta(days=1))
    assert service.morning_repick() == []
    clock.set(NOW)
    service.set_mode(rules.SICK, actor="chat")
    clock.set(NOW.replace(hour=8, minute=0) + timedelta(days=1))
    service.set_mode(rules.SICK, actor="chat")               # sick again today
    assert service.morning_repick() == []


def test_promotions_on_a_mode_day_skip_items_that_dont_fit(service, day):
    service.set_mode(rules.SICK, actor="chat")
    _, promoted = service.mark_done(day["q1"], actor="button")
    assert promoted == []                                    # only non-fitting items left
    assert service.active_count() == 2
    assert json.loads(service.meta_get(mode_key(NOW)))["mode"] == "sick"


# ---- review r1 should-fix 2: "back to normal" the same day ------------------------------------
def test_back_to_normal_same_day_repicks_the_real_three(service, day, clock):
    service.set_mode(rules.SICK, actor="button")
    clock.set(NOW.replace(hour=14, minute=0))
    before = rows(service)
    res = service.set_mode(rules.NORMAL, actor="chat")
    active = {t.id for t in service.by_status("active")}
    assert day["a"] in active                                 # the overdue deadline is back
    assert service.active_count() == 3
    assert {day["q1"], day["q2"], day["q3"]} - active        # a filler went back to the queue
    ev = db.get_event(service.conn, res.event_id)
    d = db.detail_of(ev)
    assert d["repicked"] and d["demoted"] == []
    assert "Back in your three: #%d" % day["a"] in res.text
    u = service.undo(res.event_id, actor="chat")               # one Undo restores all of it
    assert rows(service) == before and service.current_mode() == rules.SICK
    assert "re-pick that came with it is reversed too" in u.text


def test_morning_repick_runs_after_a_day_that_ended_on_normal(service, day, clock):
    service.set_mode(rules.SICK, actor="button")
    clock.set(NOW.replace(hour=14, minute=0))
    service.set_mode(rules.NORMAL, actor="chat")
    assert service.real_mode_on(NOW.date()) == "sick"
    fillers_left = {t.id for t in service.mode_fillers()}
    assert fillers_left, "the 14:00 re-pick leaves at least one filler for this test"
    clock.set(NOW.replace(hour=20, minute=0))
    late = service.add("urgent thing added tonight", area="Personal admin", place="home",
                       estimate_minutes=20, due_at=NOW.replace(hour=10) + timedelta(days=1))
    assert late.status == "queued"
    clock.set(NOW.replace(hour=8, minute=0) + timedelta(days=1))
    pairs = service.morning_repick()
    assert pairs and pairs[0][1].id == late.id and pairs[0][0].id in fillers_left
    assert service.get(late.id).status == "active"
