"""Phase 2.1 stats: total done per day (incl. did:), day mode, and the streak - a sick day with
something done keeps it; a slammed day with nothing done ends it; a no-data day is neutral."""
from datetime import datetime

from dispatch import rules, stats
from dispatch.clock import ET


def at(clock, d, h, m=0):
    clock.set(datetime(2026, 10, d, h, m, tzinfo=ET))


def post(service, clock, d, tasks):
    at(clock, d, 8)
    for i, t in enumerate(tasks, 1):
        service.log(t.id, "morning_post", actor="scheduler", rank=i, score=1.0, why="x")


def three(service, clock, d, tag):
    at(clock, d, 7)
    return [service.add(f"{tag} {i}", area="School", status="queued") for i in range(3)]


def test_streak_rules_and_new_per_day_fields(service, clock):
    # Mon 10/5 normal day: 2/3 -> good
    mon = three(service, clock, 5, "mon")
    post(service, clock, 5, mon)
    at(clock, 5, 10)
    service.mark_done(mon[0].id, actor="button")
    service.mark_done(mon[1].id, actor="button")
    # Tue 10/6 sick day: 0/3 of the three, but one did: -> good
    tue = three(service, clock, 6, "tue")
    post(service, clock, 6, tue)
    at(clock, 6, 8, 5)
    service.set_mode(rules.SICK, actor="button")
    at(clock, 6, 15)
    service.log_done("set up Dispatch", area="Coreflow/CLARA", actor="chat")
    # Wed 10/7: PC off - neutral
    # Thu 10/8 slammed, nothing done -> ends the streak (counting back from Fri)
    thu = three(service, clock, 8, "thu")
    post(service, clock, 8, thu)
    at(clock, 8, 9)
    service.set_mode(rules.SLAMMED, actor="chat")
    # Fri 10/9: 3/3 -> good
    fri = three(service, clock, 9, "fri")
    post(service, clock, 9, fri)
    at(clock, 9, 12)
    for t in fri:
        service.mark_done(t.id, actor="button")
    now = datetime(2026, 10, 9, 18, 0, tzinfo=ET)

    res = stats.compute(service.conn, now, days=5)
    days = {d["date"]: d for d in res["days"]}
    assert days["2026-10-06"]["done_total"] == 1 and days["2026-10-06"]["did"] == 1
    assert days["2026-10-06"]["mode"] == "sick" and days["2026-10-06"]["hits"] == 0
    assert stats.day_good(days["2026-10-06"]) is True
    assert stats.day_good(days["2026-10-07"]) is None
    assert stats.day_good(days["2026-10-08"]) is False
    assert res["streak"] == 1                                  # Fri only; Thu broke it
    assert stats.streak(service.conn, datetime(2026, 10, 7, 20, 0, tzinfo=ET).date()) == 2
    assert res["done_total"] == 6 and res["did_total"] == 1
    text = stats.render(res)
    assert "Tue 2026-10-06: 0/3 (0%)" in text and "done today: 1 (1 off-list) - Mode: sick" in text
    assert "Streak: 1 day(s)" in text and "Fits my day: 2 day mode(s)" in text
    assert res["extras"]["modes_set"] == 2


def test_today_in_progress_does_not_break_the_streak(service, clock):
    mon = three(service, clock, 5, "mon")
    post(service, clock, 5, mon)
    at(clock, 5, 10)
    service.mark_done(mon[0].id, actor="button")
    service.mark_done(mon[1].id, actor="button")
    tue = three(service, clock, 6, "tue")
    post(service, clock, 6, tue)                              # nothing done yet at 9 AM
    assert stats.streak(service.conn, datetime(2026, 10, 6, 9, 0, tzinfo=ET).date()) == 1
