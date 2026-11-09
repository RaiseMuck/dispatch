"""`python -m dispatch stats`: daily-three hit rate from the event log."""
import io
import json
from datetime import datetime

from dispatch import stats
from dispatch.clock import ET, FakeClock


def at(clock, d, h, m=0):
    clock.set(datetime(2026, 10, d, h, m, tzinfo=ET))


def post(service, clock, d, h, tasks, late=False):
    at(clock, d, h)
    for i, t in enumerate(tasks, 1):
        service.log(t.id, "morning_post", actor="scheduler", rank=i, score=1.0, why="x", late=late)


def done(service, clock, d, h, m, t):
    at(clock, d, h, m)
    service.mark_done(t.id, actor="button")
    return service.last_event_id


def test_hit_rates_no_data_days_and_verdict(service, clock, tmp_path):
    at(clock, 5, 7)
    mon = [service.add(f"mon {i}", area="School", status="queued") for i in range(3)]
    tue = [service.add(f"tue {i}", area="School", status="queued") for i in range(3)]
    wed = [service.add(f"wed {i}", area="School", status="queued") for i in range(3)]
    thu = service.add("thu", area="School", status="queued")
    post(service, clock, 5, 8, mon)
    done(service, clock, 5, 10, 0, mon[0])
    done(service, clock, 5, 23, 0, mon[1])                  # Mon: 2/3
    post(service, clock, 6, 8, tue)
    done(service, clock, 6, 12, 0, tue[0])                 # Tue: 1/3 ...
    undone = done(service, clock, 6, 13, 0, tue[1])
    service.undo(undone)                                   # ... an undone "done" is not a hit
    post(service, clock, 7, 13, wed, late=True)            # Wed: after-noon catch-up post
    done(service, clock, 7, 14, 0, wed[0])
    done(service, clock, 7, 20, 0, wed[1])
    done(service, clock, 8, 7, 59, wed[2])                 # before 8 AM next day: counts for Wed
    at(clock, 8, 8)
    service.log(thu.id, "morning_post", actor="scheduler", snoozes=0)   # Phase 1 style, no rank
    now = datetime(2026, 10, 8, 18, 0, tzinfo=ET)

    res = stats.compute(service.conn, now, days=4)
    rows = {d["date"]: d for d in res["days"]}
    assert [(rows[k]["hits"], rows[k]["posted"]) for k in ("2026-10-05", "2026-10-06", "2026-10-07")] \
        == [(2, 3), (1, 3), (3, 3)]
    assert rows["2026-10-08"]["status"] == "no data" and rows["2026-10-08"]["rate"] is None
    assert res["days_with_post"] == 3 and res["days_at_or_above_half"] == 2
    assert res["hit_rate_overall"] == round(6 / 9, 3)
    assert res["success_needed_days"] == 3 and res["success"] is False
    assert res["extras"]["undos"] == 1 and res["extras"]["adds_by_actor"] == {"unknown": 10}

    text = stats.render(res)
    assert "Mon 2026-10-05: 2/3 (67%)" in text and "Thu 2026-10-08: no data" in text
    assert "Success test (>= half of the daily three on >= 3 of 4 days): FAIL so far" in text

    week = stats.compute(service.conn, now, days=7)
    assert week["success_needed_days"] == 4 and len(week["days"]) == 7
    assert sum(1 for d in week["days"] if d["status"] == "no data") == 4


def test_cli_entry_json_and_text(service, clock, tmp_path):
    db_path = tmp_path / "test.db"                         # the conftest service's file
    at(clock, 5, 7)
    ts = [service.add(f"t{i}", area="School") for i in range(3)]
    post(service, clock, 5, 8, ts)
    for t in ts[:2]:
        done(service, clock, 5, 9, 0, t)
    out = io.StringIO()
    fake = FakeClock(datetime(2026, 10, 5, 20, 0, tzinfo=ET))
    assert stats.main(["--days", "1", "--json", "--db", str(db_path)], clock=fake, out=out) == 0
    res = json.loads(out.getvalue())
    assert set(res) >= {"days", "days_with_post", "days_at_or_above_half", "hit_rate_overall",
                        "success", "extras"}
    assert res["days"][0]["hits"] == 2 and res["success"] is True
    out = io.StringIO()
    stats.main(["--days", "1", "--db", str(db_path)], clock=fake, out=out)
    assert "Success test" in out.getvalue() and ": PASS" in out.getvalue()
