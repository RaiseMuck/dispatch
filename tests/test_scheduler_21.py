"""Phase 2.1 in the minute loop: flagged deadlines on a mode day, check-ins riding the hourly
message (once, never in quiet hours), the Waiting group in the Sunday review, the morning
re-pick footer, the mode row / swap row, and the split offer."""
from datetime import datetime, timedelta

from dispatch import rules, scheduler
from dispatch.clock import ET
from dispatch.scheduler import PrintPoster, RunState, tick

MON = datetime(2026, 10, 5, 8, 5, tzinfo=ET)


def run(service, clock, minutes):
    lines = []
    poster = PrintPoster(clock, out=lines.append)
    for _ in range(minutes):
        tick(service, poster, clock, RunState())
        clock.advance(timedelta(minutes=1))
    return poster.sent, lines


def test_mode_day_flags_the_deadline_that_cant_fit_in_every_hourly_message(service, clock):
    clock.set(MON - timedelta(minutes=10))
    strimer = service.add("Strimer return", place="out", estimate_minutes=45,
                          due_at=MON - timedelta(days=2))
    for i in range(3):
        service.add(f"home {i}", place="home", energy="light", estimate_minutes=20)
    clock.set(MON)
    service.meta_set(scheduler.META_MORNING, MON.date().isoformat())
    service.meta_set(scheduler.META_BATCH, (MON - timedelta(hours=2)).isoformat())
    service.set_mode(rules.SICK, actor="button")
    assert service.get(strimer.id).status == "queued"
    sent, lines = run(service, clock, 125)                  # 08:05 .. 10:09
    nags = [m for _, m in sent if m.kind == "nag"]
    assert len(nags) == 3 and all(strimer.id in [t.id for t in m.items] for m in nags)
    for m in nags:
        assert m.flags[strimer.id].startswith("can't fit today (needs going out)")
        assert m.buttons_for(service.get(strimer.id)) == rules.flagged_options()
        assert strimer.id not in m.swap_ids()
    assert any("<- can't fit today (needs going out) - pick a day?" in ln for ln in lines)


def test_checkin_rides_the_hourly_message_once_and_waits_for_8am(service, clock):
    clock.set(MON)
    t = service.add("Call Sam")
    service.set_waiting(t.id, until=datetime(2026, 10, 7, 3, 0, tzinfo=ET), actor="chat")
    clock.set(datetime(2026, 10, 7, 0, 0, tzinfo=ET))
    service.meta_set(scheduler.META_MORNING, "2026-10-06")
    sent, lines = run(service, clock, 10 * 60)              # 00:00 .. 09:59
    with_it = [(w, m) for w, m in sent if t.id in [x.id for x in m.items]]
    assert len(with_it) == 1
    when, msg = with_it[0]
    assert when == datetime(2026, 10, 7, 8, 0, tzinfo=ET) and msg.kind == "morning"
    assert msg.buttons_for(service.get(t.id)) == ["gotit", "waitmore", "kill"]
    assert "Check-in day (still waiting on someone?): #%d" % t.id in msg.footer
    assert any("[Got it] [Wait more] [Kill]" in ln for ln in lines)
    n = service.conn.execute("SELECT COUNT(*) FROM events WHERE kind='checkin_sent'").fetchone()[0]
    assert n == 1


def test_checkin_alone_in_an_hourly_message(service, clock):
    clock.set(MON)
    t = service.add("Call Sam")
    service.set_waiting(t.id, until=MON + timedelta(hours=3), actor="chat")
    service.meta_set(scheduler.META_MORNING, MON.date().isoformat())
    service.meta_set(scheduler.META_BATCH, MON.isoformat())
    clock.set(MON + timedelta(hours=3))
    sent, _ = run(service, clock, 1)
    assert len(sent) == 1 and sent[0][1].header.startswith("Still waiting on someone? Check-in day")


def test_sunday_review_lists_the_waiting_group_first(service, clock):
    clock.set(MON)
    for i in range(4):
        service.add(f"item {i}", area="School")
    service.set_waiting(1, actor="chat")
    clock.set(datetime(2026, 10, 11, 8, 0, tzinfo=ET))          # Sunday
    sent, _ = run(service, clock, 1)
    weekly = [m for _, m in sent if m.kind == "weekly"][0]
    assert weekly.body[0] == "**Waiting**" and weekly.body[1].startswith("  - #1 item 0 - check-in")
    assert "#1 " not in "\n".join(weekly.body[2:])
    assert "waiting on someone" in weekly.header


def test_morning_post_has_mode_row_swap_row_and_the_repick_footer(service, clock):
    clock.set(MON - timedelta(minutes=10))
    urgent = service.add("Strimer", place="out", due_at=MON + timedelta(days=1))
    for i in range(3):
        service.add(f"home {i}", place="home", energy="light", estimate_minutes=20)
    clock.set(MON)
    service.meta_set(scheduler.META_MORNING, MON.date().isoformat())
    service.set_mode(rules.SICK, actor="button")
    clock.set(datetime(2026, 10, 6, 7, 59, tzinfo=ET))
    sent, lines = run(service, clock, 2)
    morning = [m for _, m in sent if m.kind == "morning"][0]
    assert morning.mode_row and len(morning.swap_ids()) == 3
    assert service.get(urgent.id).status == "active"
    assert "Back after yesterday's sick/home day: #%d" % urgent.id in morning.footer
    assert any(ln.strip().startswith("mode row: [Sick/home day] [Slammed] [Normal]")
               for ln in lines)
    assert any("swap row: [Swap #" in ln for ln in lines)


def test_split_offer_for_a_big_untouched_item(service, clock):
    clock.set(MON - timedelta(days=3))
    big = service.add("PC workbench", estimate_minutes=180)
    clock.set(MON.replace(hour=7, minute=59))
    service.meta_set(scheduler.META_MORNING, "2026-10-04")
    sent, _ = run(service, clock, 2)
    morning = [m for _, m in sent if m.kind == "morning"][0]
    assert morning.split_offer == big.id and "small first step" in morning.footer
    service.meta_set(f"split_declined:{big.id}", '{"at": "%s"}' % MON.isoformat())
    assert service.split_candidate() is None               # declined this week
