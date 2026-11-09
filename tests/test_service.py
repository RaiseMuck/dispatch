"""Every mutation writes an events row; times are stored in UTC and read back in ET."""
import json
from datetime import datetime, timedelta

from dispatch import db
from dispatch.clock import ET


def kinds(service, task_id):
    return [r["kind"] for r in db.events_for(service.conn, task_id)]


def test_every_mutation_logs_an_event(service, clock):
    a = service.add("write memo section", area="School")
    assert kinds(service, a.id) == ["created"]

    service.snooze(a.id, "plus2h")
    service.snooze(a.id, "tomorrow")
    service.do_now(a.id)
    service.reschedule(a.id, clock.now() + timedelta(hours=3))
    service.mark_done(a.id)
    assert kinds(service, a.id) == ["created", "snoozed", "snoozed", "do_now", "rescheduled", "done"]

    b = service.add("old idea", area="Home & life")
    service.kill(b.id)
    assert kinds(service, b.id) == ["created", "killed"]

    detail = json.loads(db.events_for(service.conn, a.id)[1]["detail"])
    assert detail["choice"] == "plus2h" and detail["count"] == 1


def test_promotion_logs_an_event(service):
    a = [service.add(f"t{i}", area="School") for i in range(3)]
    q = service.add("waiting", area="School")
    service.mark_done(a[0].id)
    assert kinds(service, q.id) == ["created", "promoted"]


def test_times_stored_utc_read_back_eastern(service):
    due = datetime(2026, 10, 3, 11, 0, tzinfo=ET)
    t = service.add("return the cable", area="Personal admin", due_at=due)
    raw = service.conn.execute("SELECT due_at FROM tasks WHERE id=?", (t.id,)).fetchone()[0]
    assert raw == "2026-10-03T15:00:00+00:00"
    assert t.due_at == due and t.due_at.tzinfo is ET


def test_closed_task_buttons_are_refused(service):
    import pytest
    from dispatch.service import NotOpen
    t = service.add("x", area="School")
    service.mark_done(t.id)
    with pytest.raises(NotOpen):
        service.snooze(t.id, "plus2h")


def test_migrations_are_versioned_and_idempotent(service, clock):
    assert db.current_version(service.conn) == 4        # Phase 2.1 adds 0004_fits_my_day.sql
    assert db.migrate(service.conn, clock.now()) == []


def test_import_seed_loads_items(service):
    from dispatch.__main__ import SEED_PATH, import_seed
    n = import_seed(service, SEED_PATH)
    assert 10 <= n <= 20
    assert import_seed(service, SEED_PATH) == 0            # second run adds nothing
    strimer = [t for t in service.by_status("active") if "Strimer" in t.title]
    assert strimer and strimer[0].due_at == datetime(2026, 10, 3, 11, 0, tzinfo=ET)
    assert strimer[0].area == "Personal admin"
    assert service.active_count() == 3
    parked = service.by_status("parked")               # the seed row marked "parked" stays parked
    assert [t.title for t in parked] == [
        "Pick the real target-margin cell (or set a target) in Phase1_Agent_Ledger.xlsx"]
    assert all(t.notes and "backlog.md L" in t.notes
               for t in service.by_status("active", "queued", "parked"))
