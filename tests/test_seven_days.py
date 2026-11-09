"""Phase 1's one promise: nothing snoozed gets forgotten for 7 days.

A scripted Max never finishes anything. Every time an item is waiting on him he climbs the
ladder: +2h, then Tonight, then (snooze buttons gone) Pick a day -> tomorrow 10 AM, which
pushes it out of the active three into the queue; it comes due again the next day and the
ladder starts over. On Wednesday the PC is off 07:00-13:00, so the 8 AM post never happens.
"""
from collections import Counter
from datetime import datetime, timedelta

from dispatch import db, rules
from dispatch.clock import ET
from dispatch.parser import parse_when
from dispatch.scheduler import PrintPoster, RunState, tick

START = datetime(2026, 9, 28, 7, 0, tzinfo=ET)          # Monday
END = START + timedelta(days=7)
PC_OFF = (datetime(2026, 9, 30, 7, 0, tzinfo=ET), datetime(2026, 9, 30, 13, 0, tzinfo=ET))


def simulate(service, clock):
    clock.set(START)
    tasks = [service.add("Call Sam at Acme Plumbing", area="Coreflow/CLARA"),
             service.add("Write memo section 3", area="School"),
             service.add("Decide on the bike", area="Portfolio"),
             service.add("Plan the garden", area="Home & life")]      # 4th starts queued, undated
    poster = PrintPoster(clock, out=lambda *_: None)
    state = RunState()
    taps = Counter()
    catchup_expected = None
    while clock.now() < END:
        now = clock.now()
        if not (PC_OFF[0] <= now < PC_OFF[1]):
            for msg in tick(service, poster, clock, state):
                if msg.kind == "catchup":
                    catchup_expected = ({t.id for t in service.by_status("active")}
                                        | {t.id for t in service.due_batch(now)})
                for shown in msg.items:
                    t = service.get(shown.id)
                    if not rules.is_due(t, now):
                        continue                      # listed, but not waiting on an answer
                    if t.snooze_count == 0:
                        service.snooze(t.id, rules.PLUS2H)
                        taps["+2h"] += 1
                    elif t.snooze_count == 1:
                        service.snooze(t.id, rules.TONIGHT)
                        taps["tonight"] += 1
                    else:
                        assert rules.snooze_options(t.snooze_count) == \
                            [rules.DO_NOW, rules.PICK_DAY, rules.KILL]
                        service.reschedule(t.id, parse_when("tomorrow 10am", now))
                        taps["pick a day"] += 1
        clock.advance(timedelta(minutes=1))
    return tasks, poster.sent, taps, catchup_expected


def test_nothing_is_forgotten_for_seven_days(service, clock):
    tasks, sent, taps, catchup_expected = simulate(service, clock)
    ids = [t.id for t in tasks]
    assert [service.get(i).status in ("active", "queued") for i in ids] == [True] * 4

    # No message in quiet hours, none while the PC was off.
    assert not [w for w, _ in sent if rules.in_quiet_hours(w)]
    assert not [w for w, _ in sent if PC_OFF[0] <= w < PC_OFF[1]]

    for tid in ids:
        seen = sorted(w for w, m in sent if tid in [t.id for t in m.shown()])
        # (a) appears on every calendar day of the week, always outside quiet hours
        days = {w.astimezone(ET).date() for w in seen}
        for d in range(7):
            day = (START + timedelta(days=d)).date()
            assert day in days, f"#{tid} never shown on {day}"
        # (b) never absent for more than 24 hours, from the first minute to the last
        points = [START] + seen + [END]
        worst = max(b - a for a, b in zip(points, points[1:]))
        assert worst <= timedelta(hours=24), f"#{tid} silent for {worst}"

    # The full ladder really ran, many times, including re-queueing.
    assert taps["+2h"] >= 20 and taps["tonight"] >= 20 and taps["pick a day"] >= 20
    freed = service.conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind='rescheduled' AND detail LIKE '%\"freed_slot\": true%'"
    ).fetchone()[0]
    assert freed >= 7
    for tid in ids:
        kinds = [r["kind"] for r in db.events_for(service.conn, tid)]
        assert kinds.count("rescheduled") >= 5          # re-queued/re-due at least 5 times
        assert kinds.count("snoozed") >= 10


def test_pc_off_all_morning_items_still_surface_that_day(service, clock):
    tasks, sent, _, catchup_expected = simulate(service, clock)
    wednesday = [(w, m) for w, m in sent if w.astimezone(ET).date() == PC_OFF[0].date()]
    first_at, first = wednesday[0]
    assert first_at == PC_OFF[1]                       # first tick after the PC comes back
    assert first.kind == "catchup"                     # no 8 AM post that day...
    assert not [m for _, m in wednesday if m.kind == "morning"]
    shown = {t.id for t in first.items}
    assert catchup_expected and catchup_expected <= shown   # ...but all 3 active + everything due
    for t in tasks:                                     # and every item shows up on Wednesday
        assert any(t.id in [x.id for x in m.shown()] for _, m in wednesday)
