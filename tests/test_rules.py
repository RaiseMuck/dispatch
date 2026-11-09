"""Snooze ladder, active cap, quiet hours (incl. the 2026-11-01 DST change), ordering."""
from datetime import datetime, timedelta, timezone

import pytest

from dispatch import rules
from dispatch.clock import ET
from dispatch.service import LadderError

UTC = timezone.utc


# ---- snooze ladder -----------------------------------------------------------------------
def test_first_two_snoozes_offer_snooze_buttons():
    for n in (0, 1):
        assert rules.snooze_options(n) == ["done", "plus2h", "tonight", "tomorrow", "kill"]


def test_third_snooze_offers_exactly_do_now_pick_a_day_kill():
    opts = rules.snooze_options(2)
    assert [rules.LABELS[o] for o in opts] == ["Do now", "Pick a day", "Kill"]
    assert not set(opts) & {"plus2h", "tonight", "tomorrow"}
    assert rules.snooze_options(5) == opts


def test_ladder_enforced_by_service(service):
    t = service.add("pay parking ticket", area="Personal admin")
    service.snooze(t.id, "plus2h")
    t = service.snooze(t.id, "tonight")
    assert t.snooze_count == 2
    assert [rules.LABELS[o] for o in rules.snooze_options(t.snooze_count)] == \
        ["Do now", "Pick a day", "Kill"]
    for choice in ("plus2h", "tonight", "tomorrow"):
        with pytest.raises(LadderError):
            service.snooze(t.id, choice)


def test_resolve_snooze_times():
    now = datetime(2026, 9, 28, 14, 10, tzinfo=ET)
    assert rules.resolve_snooze("plus2h", now) == datetime(2026, 9, 28, 16, 10, tzinfo=ET)
    assert rules.resolve_snooze("tonight", now) == datetime(2026, 9, 28, 20, 0, tzinfo=ET)
    late = datetime(2026, 9, 28, 21, 0, tzinfo=ET)
    assert rules.resolve_snooze("tonight", late) == datetime(2026, 9, 29, 20, 0, tzinfo=ET)
    assert rules.resolve_snooze("tomorrow", now) == datetime(2026, 9, 29, 9, 0, tzinfo=ET)
    explicit = datetime(2026, 10, 1, 13, 0, tzinfo=ET)
    assert rules.resolve_snooze("plus2h", now, explicit) == explicit


# ---- active cap --------------------------------------------------------------------------
def test_fourth_add_is_queued_and_done_promotes_it(service):
    a = [service.add(f"task {i}", area="School") for i in range(3)]
    assert all(t.status == "active" for t in a)
    fourth = service.add("task 4", area="School")
    assert fourth.status == "queued"
    assert service.active_count() == 3
    done, promoted = service.mark_done(a[0].id)
    assert done.status == "done"
    assert [p.id for p in promoted] == [fourth.id]
    assert service.get(fourth.id).status == "active"
    assert service.active_count() == 3


def test_kill_and_reschedule_out_free_a_slot(service, clock):
    a = [service.add(f"task {i}", area="School") for i in range(3)]
    q1 = service.add("queued 1", area="Portfolio")
    q2 = service.add("queued 2", area="Coreflow/CLARA")
    _, promoted = service.kill(a[0].id)
    assert [p.id for p in promoted] == [q2.id]          # heavier area goes first
    moved, promoted = service.reschedule(a[1].id, clock.now() + timedelta(days=2))
    assert moved.status == "queued"
    assert [p.id for p in promoted] == [q1.id]
    assert service.active_count() == 3


def test_area_alone_never_parks_but_explicit_status_does(service):
    t = service.add("fantasy highlight reel app", area="Side-venture ideas")
    assert t.status == "active"                       # area does not decide status
    p = service.add("some idea", area="Side-venture ideas", status="parked")
    assert p.status == "parked" and p.next_nag_at is None
    assert service.active_count() == 1


# ---- quiet hours -------------------------------------------------------------------------
def test_quiet_hours_both_1_30s_on_dst_day():
    # 2026-11-01: clocks fall back at 02:00 EDT, so 01:30 happens twice.
    first = datetime(2026, 11, 1, 5, 30, tzinfo=UTC)    # 01:30 EDT (UTC-4)
    second = datetime(2026, 11, 1, 6, 30, tzinfo=UTC)   # 01:30 EST (UTC-5)
    assert first.astimezone(ET).strftime("%H:%M %Z") == "01:30 EDT"
    assert second.astimezone(ET).strftime("%H:%M %Z") == "01:30 EST"
    assert rules.in_quiet_hours(first)
    assert rules.in_quiet_hours(second)


def test_quiet_hours_edges():
    assert rules.in_quiet_hours(datetime(2026, 9, 28, 0, 0, tzinfo=ET))
    assert rules.in_quiet_hours(datetime(2026, 9, 28, 7, 59, tzinfo=ET))
    assert not rules.in_quiet_hours(datetime(2026, 9, 28, 8, 0, tzinfo=ET))
    assert not rules.in_quiet_hours(datetime(2026, 9, 28, 23, 59, tzinfo=ET))
    # DST morning: 07:59 EST is quiet, 08:00 EST is not
    assert rules.in_quiet_hours(datetime(2026, 11, 1, 12, 59, tzinfo=UTC))
    assert not rules.in_quiet_hours(datetime(2026, 11, 1, 13, 0, tzinfo=UTC))


def test_next_allowed_moves_quiet_times_to_8am():
    assert rules.next_allowed(datetime(2026, 11, 1, 6, 30, tzinfo=UTC)) == \
        datetime(2026, 11, 1, 8, 0, tzinfo=ET)
    t = datetime(2026, 9, 28, 15, 0, tzinfo=ET)
    assert rules.next_allowed(t) == t


def test_next_batch_time_spacing():
    last = datetime(2026, 9, 28, 9, 0, tzinfo=ET)
    assert rules.next_batch_time(last, last + timedelta(minutes=5)) == last + timedelta(hours=1)
    late = datetime(2026, 9, 28, 23, 30, tzinfo=ET)
    assert rules.next_batch_time(late, late) == datetime(2026, 9, 29, 8, 0, tzinfo=ET)


def test_morning_items_follow_rank(service):   # was test_morning_order_due_then_weight
    # Phase 2 (plan 3.9): the morning order is rules.rank, no longer "due first".
    # now = Mon 9/28 09:00. Defaults: payoff 3, likelihood 0.7, 30 min (0.5 h).
    #   undated Coreflow (22):   3 x 0.7 / 0.5 x 2.2 x 1.0                        = 9.24
    #   return, Personal admin (8), due Sat 10/3 11:00 = 122 h away -> boost
    #     1 + (168 - 122) / 120 = 1.383;  3 x 0.7 / 0.5 x 0.8 x 1.383             = 4.65
    #   undated Home & life (5): 3 x 0.7 / 0.5 x 0.5 x 1.0                        = 2.10
    # Not within 48 h, so Guardrail 2 does not lift the dated one above the Coreflow item.
    undated_heavy = service.add("coreflow thing", area="Coreflow/CLARA")
    undated_light = service.add("garden", area="Home & life")
    dated = service.add("return", area="Personal admin",
                        due_at=datetime(2026, 10, 3, 11, 0, tzinfo=ET))
    now = service.clock.now()
    assert round(rules.score(undated_heavy, now), 2) == 9.24
    assert round(rules.score(dated, now), 2) == 4.65
    assert round(rules.score(undated_light, now), 2) == 2.10
    assert [t.id for t in service.morning_items()] == [undated_heavy.id, dated.id, undated_light.id]
