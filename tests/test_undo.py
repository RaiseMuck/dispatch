"""Undo exactly reverses each kind of change (full task rows compared), including the promotion
a done/kill/reschedule caused; it refuses when the item changed since, after 48 h, or twice."""
from datetime import datetime, timedelta

import pytest

from dispatch import db, rules
from dispatch.clock import ET
from dispatch.service import UndoRefused

NOW = datetime(2026, 10, 5, 10, 0, tzinfo=ET)


def row(service, tid):
    return tuple(service.conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())


def rows(service):
    return [tuple(r) for r in service.conn.execute("SELECT * FROM tasks ORDER BY id")]


def undone_by(service, eid):
    return service.conn.execute("SELECT undone_by FROM events WHERE id=?", (eid,)).fetchone()[0]


@pytest.fixture
def four(service, clock):
    """3 active (one with guessed fields and a due date) + 2 queued."""
    clock.set(NOW)
    a = service.add("alpha", area="School", guessed=["payoff", "estimate_minutes"], payoff=4,
                    estimate_minutes=45, due_at=NOW + timedelta(days=3))
    b = service.add("bravo", area="Portfolio")
    c = service.add("charlie", area="Home & life")
    q1 = service.add("queued heavy", area="Coreflow/CLARA")
    q2 = service.add("queued light", area="Home & life")
    clock.advance(timedelta(minutes=5))
    return a, b, c, q1, q2


@pytest.mark.parametrize("act", [
    lambda s, a: s.snooze(a.id, rules.PLUS2H, actor="chat"),
    lambda s, a: s.snooze(a.id, rules.TOMORROW, actor="button"),
    lambda s, a: s.do_now(a.id, actor="button"),
    lambda s, a: s.reschedule(a.id, NOW + timedelta(hours=3), actor="chat"),       # same day
    lambda s, a: s.set_fields(a.id, actor="chat", estimate_minutes=180),
    lambda s, a: s.set_fields(a.id, actor="chat", payoff=2),
    lambda s, a: s.set_fields(a.id, actor="chat", likelihood=0.3),
    lambda s, a: s.set_fields(a.id, actor="chat", area="Personal admin"),
    lambda s, a: s.set_fields(a.id, actor="cli", payoff=5, estimate_minutes=10),
], ids=["snooze+2h", "snooze-tomorrow", "do_now", "reschedule-in-day", "estimated",
        "prioritized", "likelihood", "area_set", "mixed"])
def test_single_task_changes_undo_to_the_exact_row(service, four, act):
    a = four[0]
    before = rows(service)
    act(service, a)
    eid = service.last_event_id
    assert rows(service) != before
    res = service.undo(eid, actor="chat")
    assert rows(service) == before
    assert undone_by(service, eid) == res.event_id
    kind = service.conn.execute("SELECT kind, actor FROM events WHERE id=?", (res.event_id,)).fetchone()
    assert tuple(kind) == ("undone", "chat")


def test_set_fields_clears_guessed_and_undo_restores_it(service, four):
    a = four[0]
    t = service.set_fields(a.id, actor="chat", estimate_minutes=180)
    assert t.guessed == ["payoff"] and t.estimate_minutes == 180
    service.undo(service.last_event_id)
    t = service.get(a.id)
    assert t.guessed == ["payoff", "estimate_minutes"] and t.estimate_minutes == 45


@pytest.mark.parametrize("how", ["done", "kill", "reschedule_out"])
def test_close_or_move_out_with_promotion_is_fully_reversed(service, four, how):
    a, b, c, q1, q2 = four
    before = rows(service)
    if how == "done":
        _, promoted = service.mark_done(b.id, actor="button")
    elif how == "kill":
        _, promoted = service.kill(b.id, actor="chat")
    else:
        _, promoted = service.reschedule(b.id, NOW + timedelta(days=4), actor="chat")
    eid = service.last_event_id
    assert [p.id for p in promoted] == [q1.id]           # heavier queued item moved up
    res = service.undo(eid)
    assert rows(service) == before                        # b back, q1 back in the queue
    promo = service.conn.execute("SELECT id, undone_by FROM events WHERE kind='promoted' "
                                 "AND cause_id=?", (eid,)).fetchone()
    assert promo["undone_by"] == res.event_id
    assert f"#{q1.id} goes back to the queue" in res.text
    assert service.active_count() == 3


def test_promoted_task_touched_since_stays_active(service, four):
    a, b, c, q1, q2 = four
    service.mark_done(b.id, actor="button")
    done_eid = service.last_event_id
    service.snooze(q1.id, rules.PLUS2H, actor="button")  # Max already acted on the promoted one
    res = service.undo(done_eid)
    assert service.get(b.id).status == "active"
    assert service.get(q1.id).status == "active"          # kept; the cap self-corrects later
    assert f"#{q1.id} stays active" in res.text


def test_undo_of_an_add_kills_it_and_refills_the_slot(service, clock):
    clock.set(NOW)
    x = [service.add(f"x{i}", area="School") for i in range(2)]
    waiting = service.add("third", area="School")
    q = service.add("queued", area="School")
    assert (waiting.status, q.status) == ("active", "queued")
    res = service.undo(db.events_for(service.conn, waiting.id)[0]["id"])
    t = service.get(waiting.id)
    assert t.status == "killed" and t.closed_at is not None and t.next_nag_at is None
    assert service.get(q.id).status == "active" and [p.id for p in res.promoted] == [q.id]
    assert service.get(waiting.id) is not None             # never deleted: history kept


def test_shift_after_undo_restores_every_task(service, four, clock):
    before = rows(service)
    moved, eid = service.shift_after(NOW.replace(hour=16), actor="chat")
    assert {t.id for t in moved} == {four[1].id, four[2].id}   # alpha is due in 3 days; queued have no nag
    assert all(t.next_nag_at == NOW.replace(hour=16) for t in moved)
    service.undo(eid)
    assert rows(service) == before


def test_refusals(service, four, clock):
    a, b = four[0], four[1]
    service.snooze(a.id, rules.PLUS2H, actor="chat")
    snooze_eid = service.last_event_id
    service.mark_done(a.id, actor="button")
    with pytest.raises(UndoRefused, match=r"marked done at .* after that"):
        service.undo(snooze_eid)                           # later change -> ask, never unwind
    done_eid = service.last_event_id
    res = service.undo(done_eid)
    with pytest.raises(UndoRefused, match="already undone"):
        service.undo(done_eid)
    with pytest.raises(UndoRefused, match="undo an undo"):
        service.undo(res.event_id)
    service.record_nags([service.get(b.id)])
    nag = service.conn.execute("SELECT MAX(id) FROM events WHERE kind='nag_sent'").fetchone()[0]
    with pytest.raises(UndoRefused, match="isn't a change"):
        service.undo(nag)
    with pytest.raises(UndoRefused, match="can't find"):
        service.undo(99999)


@pytest.mark.parametrize("hours,ok", [(47, True), (49, False)])
def test_undo_window_is_48_hours(service, four, clock, hours, ok):
    service.snooze(four[1].id, rules.PLUS2H, actor="button")
    eid = service.last_event_id
    clock.advance(timedelta(hours=hours))
    if ok:
        service.undo(eid)
    else:
        with pytest.raises(UndoRefused, match="48 hours"):
            service.undo(eid)


def test_latest_undoable_only_max_actions(service, four, clock):
    a, b, c, q1, q2 = four
    service.snooze(a.id, rules.PLUS2H, actor="button")
    mine = service.last_event_id
    service.set_fields(c.id, actor="cli", payoff=5)        # a Claude/CLI change is not "that"
    assert service.latest_undoable() == mine
    service.undo(mine)
    assert service.latest_undoable() is None
    clock.advance(timedelta(hours=49))
    service.snooze(b.id, rules.PLUS2H, actor="chat")
    assert service.latest_undoable() == service.last_event_id


def test_phase1_rows_without_before_are_refused(service, four):
    ev = db.log_event(service.conn, four[0].id, "snoozed", NOW, choice="plus2h")
    service.conn.commit()
    with pytest.raises(UndoRefused, match="before Undo existed"):
        service.undo(ev)
