"""Phase 2.1: 'did:' logs finished off-list work; a big item can be split into a first step;
Start on a best-fit suggestion. All undoable."""
from datetime import datetime, timedelta

import pytest

from dispatch import db
from dispatch.clock import ET
from dispatch.service import CantDo, UndoRefused, describe

NOW = datetime(2026, 10, 5, 10, 0, tzinfo=ET)


def rows(service):
    return [tuple(r) for r in service.conn.execute("SELECT * FROM tasks ORDER BY id")]


@pytest.fixture
def three(service, clock):
    clock.set(NOW)
    ids = [service.add(f"item {i}", area="Personal admin", estimate_minutes=30).id
           for i in range(3)]
    ids.append(service.add("big job", area="Personal admin", estimate_minutes=180).id)
    return ids


def test_did_creates_a_done_row_and_undo_kills_it(service, three):
    before_active = service.active_count()
    t = service.log_done("set up Dispatch", area="Coreflow/CLARA", actor="chat")
    assert t.status == "done" and t.closed_at == NOW and service.active_count() == before_active
    evs = db.events_for(service.conn, t.id)
    assert [e["kind"] for e in evs] == ["created", "done"]
    assert db.detail_of(evs[0])["did"] is True and evs[1]["cause_id"] == evs[0]["id"]
    assert service.last_event_id == evs[0]["id"]
    u = service.undo(evs[0]["id"], actor="chat")
    assert service.get(t.id).status == "killed"
    assert all(e["undone_by"] == u.event_id for e in db.events_for(service.conn, t.id)[:2])


def test_split_puts_the_step_in_the_parents_slot(service, three):
    big = three[3]
    service.start(big, actor="button")                      # big is now one of the three
    parent_before = service.get(big)
    before = rows(service)
    child, parent, sid = service.split(big, "Outline the three sections", 15, actor="button")
    assert child.status == "active" and child.parent_id == big and child.estimate_minutes == 15
    assert child.energy == "light" and parent.status == "queued"
    assert service.active_count() == 3
    assert "(step of #%d)" % big in describe(child)
    created = db.events_for(service.conn, child.id)[0]
    assert created["cause_id"] == sid
    u = service.undo(sid, actor="button")
    assert service.get(child.id).status == "killed"
    assert service.get(big).status == parent_before.status
    assert [r for r in rows(service) if r[0] != child.id] == before
    assert "The step" in u.text


def test_split_undo_refused_once_the_step_was_touched(service, three):
    child, _, sid = service.split(three[0], "first bit", 10, actor="button")
    service.mark_done(child.id, actor="button")
    with pytest.raises(UndoRefused, match="first step"):
        service.undo(sid)


def test_split_minutes_are_clamped(service, three):
    child, _, _ = service.split(three[0], "x", 45, actor="button")
    assert child.estimate_minutes == 20


def test_start_on_active_is_do_now(service, three, clock):
    r = service.start(three[1], actor="button")
    assert r.demoted is None and r.task.next_nag_at == NOW + timedelta(hours=1)


def test_start_on_queued_demotes_the_lowest_ranked_non_urgent(service, three, clock):
    service.conn.execute("UPDATE tasks SET due_at=? WHERE id=?",
                         (db.to_db(NOW + timedelta(hours=5)), three[0]))
    service.conn.commit()
    before = rows(service)
    r = service.start(three[3], actor="button")
    assert r.task.status == "active" and r.demoted is not None and r.demoted.id != three[0]
    assert r.demoted.status == "queued" and service.active_count() == 3
    service.undo(r.event_id, actor="button")
    assert rows(service) == before


def test_start_refused_when_all_three_are_urgent(service, three):
    service.conn.execute("UPDATE tasks SET due_at=? WHERE status='active'",
                         (db.to_db(NOW + timedelta(hours=5)),))
    service.conn.commit()
    with pytest.raises(CantDo, match="All three"):
        service.start(three[3], actor="button")
