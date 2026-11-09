"""Phase 2.1 'waiting on someone': no slot, one check-in, Got it / Wait more / Kill, Undo."""
from datetime import datetime, timedelta

import pytest

from dispatch import rules
from dispatch.clock import ET
from dispatch.service import CantDo, NotActive, describe

NOW = datetime(2026, 10, 5, 10, 0, tzinfo=ET)


def rows(service):
    return [tuple(r) for r in service.conn.execute("SELECT * FROM tasks ORDER BY id")]


@pytest.fixture
def w(service, clock):
    clock.set(NOW)
    ids = [service.add(f"item {i}", area="Personal admin").id for i in range(5)]
    return ids                      # 0-2 active, 3-4 queued


def test_set_waiting_frees_the_slot_and_hides_the_item(service, w, clock):
    t, promoted = service.set_waiting(w[0], actor="chat")
    assert t.status == "waiting" and t.waiting_until == NOW + timedelta(days=3)
    assert [p.id for p in promoted] == [w[3]] and service.active_count() == 3
    clock.advance(timedelta(days=1))
    assert w[0] not in {x.id for x in service.due_batch()}
    assert w[0] not in {x.id for x in service.morning_items()}
    assert w[0] in {x.id for x in service.open_tasks()}
    assert "waiting, check-in Thu 10/8" in describe(service.get(w[0]))


def test_checkin_fires_once_at_its_moment(service, w, clock):
    until = NOW + timedelta(days=2)
    service.set_waiting(w[3], until=until, actor="chat")      # a queued item: no slot involved
    clock.set(until - timedelta(minutes=1))
    assert service.due_checkins() == []
    clock.set(until)
    due = service.due_checkins()
    assert [t.id for t in due] == [w[3]]
    service.record_nags(due, kind="checkin_sent")
    clock.advance(timedelta(hours=5))
    assert service.due_checkins() == []


def test_got_it_returns_it_to_the_queue_due_now(service, w, clock):
    service.set_waiting(w[0], actor="chat")
    clock.advance(timedelta(days=3))
    before = rows(service)
    t, promoted = service.resume_waiting(w[0], actor="button")
    assert t.status == "queued" and t.waiting_until is None and t.next_nag_at == clock.now()
    assert t in service.due_batch()                            # never lost
    service.undo(service.last_event_id, actor="button")
    assert rows(service) == before


def test_wait_more_twice_then_refused(service, w, clock):
    service.set_waiting(w[0], actor="chat")
    for i in range(2):
        clock.advance(timedelta(days=3))
        t = service.wait_more(w[0], actor="button")
        assert t.waiting_until == clock.now() + timedelta(days=3)
    assert service.wait_more_count(w[0]) == 2
    assert rules.waiting_options(service.wait_more_count(w[0])) == ["gotit", "pickday", "kill"]
    with pytest.raises(CantDo, match="extended twice"):
        service.wait_more(w[0], actor="button")
    # Pick a day ends the waiting
    t, _ = service.reschedule(w[0], clock.now() + timedelta(days=2), actor="button")
    assert t.status == "queued" and t.waiting_until is None


def test_waiting_items_cannot_be_snoozed_but_can_be_closed(service, w):
    service.set_waiting(w[0], actor="chat")
    with pytest.raises(NotActive):
        service.snooze(w[0], rules.PLUS2H)
    with pytest.raises(NotActive):
        service.do_now(w[0])
    t, promoted = service.kill(w[0], actor="button")
    assert t.status == "killed" and t.waiting_until is None and promoted == []


def test_undo_of_waiting_and_of_wait_more(service, w, clock):
    before = rows(service)
    service.set_waiting(w[1], actor="chat")
    eid = service.last_event_id
    service.undo(eid, actor="chat")
    assert rows(service) == before
    service.set_waiting(w[1], actor="chat")
    mid = rows(service)
    service.wait_more(w[1], actor="button")
    service.undo(service.last_event_id, actor="button")
    assert rows(service) == mid


def test_unanswered_checkin_comes_back_weekly(service, w, clock):
    """Review r1 fix 6: after the first check-in, an unanswered waiting item is pinged again
    once a week (still riding the hourly message), never more often."""
    from dispatch import scheduler
    from dispatch.scheduler import PrintPoster, RunState, tick
    until = NOW + timedelta(days=3)
    service.set_waiting(w[3], until=until, actor="chat")
    service.meta_set(scheduler.META_MORNING, "2026-10-99")      # no morning post in this test
    poster = PrintPoster(clock, out=lambda *_: None)

    def pings(hours):
        n = 0
        for _ in range(hours * 60):
            for m in tick(service, poster, clock, RunState()):
                n += w[3] in [t.id for t in m.items]
            clock.advance(timedelta(minutes=1))
        return n

    clock.set(until)
    assert pings(1) == 1                                         # first check-in
    service.meta_set(scheduler.META_MORNING, "2026-10-99")
    clock.set(until + timedelta(days=6, hours=23))
    assert service.due_checkins() == []                           # not within the week
    clock.set(until + timedelta(days=7))
    assert [t.id for t in service.due_checkins()] == [w[3]]       # a week later: again
    service.record_nags(service.due_checkins(), kind="checkin_sent")
    clock.advance(timedelta(days=1))
    assert service.due_checkins() == []
    service.wait_more(w[3], actor="button")                       # answering resets the cycle
    assert service.due_checkins() == []
