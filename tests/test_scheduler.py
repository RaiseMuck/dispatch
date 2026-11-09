"""Batched hourly nags, quiet hours in the loop, 08:00 release, morning post, heartbeat."""
import json
from datetime import datetime, timedelta, timezone

from dispatch import rules, scheduler
from dispatch.clock import ET
from dispatch.scheduler import PrintPoster, RunState, tick

UTC = timezone.utc


def run_minutes(service, clock, minutes, state=None):
    poster = PrintPoster(clock, out=lambda *_: None)
    state = state or RunState()
    for _ in range(minutes):
        tick(service, poster, clock, state)
        clock.advance(timedelta(minutes=1))
    return poster.sent


def test_five_due_items_make_one_batched_nag_and_nothing_within_the_hour(service, clock):
    clock.set(datetime(2026, 9, 28, 13, 0, tzinfo=ET))
    service.meta_set(scheduler.META_MORNING, "2026-09-28")   # morning post already done today
    due = datetime(2026, 9, 28, 13, 0, tzinfo=ET)
    ids = [service.add(f"item {i}", area="School", due_at=due).id for i in range(5)]
    assert len(service.due_batch()) == 5                      # 3 active + 2 queued, all past due

    sent = run_minutes(service, clock, 61)                    # 13:00 .. 14:00 inclusive
    nags = [(w, m) for w, m in sent if m.kind == "nag"]
    first_at, first = nags[0]
    assert first_at == datetime(2026, 9, 28, 13, 0, tzinfo=ET)
    assert sorted(t.id for t in first.items) == sorted(ids)   # all five in ONE message
    within_hour = [w for w, _ in nags if first_at < w < first_at + timedelta(minutes=60)]
    assert within_hour == []
    assert len(nags) == 2 and nags[1][0] == first_at + timedelta(hours=1)   # re-listed at 14:00
    rows = service.conn.execute("SELECT COUNT(*) FROM events WHERE kind='nag_sent'").fetchone()[0]
    assert rows == 10                                          # 5 items x 2 batches


def test_item_due_overnight_waits_for_8am(service, clock):
    clock.set(datetime(2026, 9, 29, 2, 0, tzinfo=ET))
    t = service.add("overnight thing", area="School", due_at=datetime(2026, 9, 29, 3, 0, tzinfo=ET))
    sent = run_minutes(service, clock, 6 * 60 + 30)            # 02:00 .. 08:29
    assert sent, "expected the 08:00 message"
    first_at, first = sent[0]
    assert first_at == datetime(2026, 9, 29, 8, 0, tzinfo=ET)
    assert first.kind == "morning" and t.id in [x.id for x in first.items]
    assert all(w.astimezone(ET).hour >= 8 for w, _ in sent)


def test_dst_night_is_silent_and_morning_is_8am_est(service, clock):
    clock.set(datetime(2026, 11, 1, 0, 0, tzinfo=ET))           # 00:00 EDT
    service.add("due during the repeated hour", area="School",
                due_at=datetime(2026, 11, 1, 5, 30, tzinfo=UTC))   # 01:30 EDT
    service.add("due during the second 1:30", area="School",
                due_at=datetime(2026, 11, 1, 6, 30, tzinfo=UTC))   # 01:30 EST
    sent = run_minutes(service, clock, 9 * 60 + 5)             # 9 real hours = 00:00 EDT .. 08:04 EST
    # 2026-11-01 is a Sunday, so the weekly review also goes out - at the same 08:00 EST.
    assert [(w, m.kind) for w, m in sent] == [(datetime(2026, 11, 1, 13, 0, tzinfo=UTC), "morning"),
                                              (datetime(2026, 11, 1, 13, 0, tzinfo=UTC), "weekly")]
    assert len(sent[0][1].items) == 2


def test_morning_post_once_per_day_three_items_in_order(service, clock):
    clock.set(datetime(2026, 9, 28, 7, 0, tzinfo=ET))
    service.add("undated heavy", area="Coreflow/CLARA")
    service.add("soon", area="Personal admin", due_at=datetime(2026, 10, 3, 11, 0, tzinfo=ET))
    service.add("later", area="School", due_at=datetime(2026, 10, 17, 9, 0, tzinfo=ET))
    service.add("queued one", area="Portfolio")
    sent = run_minutes(service, clock, 3 * 60)                 # 07:00 .. 09:59
    mornings = [(w, m) for w, m in sent if m.kind == "morning"]
    assert len(mornings) == 1
    w, m = mornings[0]
    assert w == datetime(2026, 9, 28, 8, 0, tzinfo=ET)
    # Phase 2 (plan 3.9): rank order. At Mon 9/28 08:00 with default payoff/likelihood/estimate:
    #   undated heavy (Coreflow 22): 3 x 0.7 / 0.5 x 2.2                     = 9.24
    #   later (School 18, due 10/17 = 457 h away, boost 1.0): 4.2 x 1.8      = 7.56
    #   soon (Personal admin 8, due 10/3 11:00 = 123 h, boost 1.375): 4.2 x 0.8 x 1.375 = 4.62
    assert [t.title for t in m.items] == ["undated heavy", "later", "soon"]
    assert [t.id for t in m.items] == [t.id for t in rules.rank(m.items, w)]
    assert not [x for x, _ in sent if x < datetime(2026, 9, 28, 8, 0, tzinfo=ET)]


def test_heartbeat_written_every_tick(service, clock, tmp_path):
    hb = tmp_path / "heartbeat.json"
    run_minutes(service, clock, 2, RunState(heartbeat_path=hb, bot_connected=True))
    data = json.loads(hb.read_text(encoding="utf-8"))
    assert data["bot_connected"] is True and data["version"]
    assert datetime.fromisoformat(data["last_loop_at"]).utcoffset() == timedelta(hours=-4)
