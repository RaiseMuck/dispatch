"""Phase 2 ranking: score, defaults, the deadline boost, Guardrail 2, tie-breaks, and where rank
decides (promotion into a freed slot, the order of the 08:00 three)."""
import json
from dataclasses import dataclass
from datetime import datetime, timedelta

import pytest

from dispatch import db, rules
from dispatch.clock import ET
from dispatch.scheduler import PrintPoster, RunState, tick

NOW = datetime(2026, 10, 5, 9, 0, tzinfo=ET)


@dataclass
class T:                                   # the fields rank() reads
    id: int
    area: str = "School"
    due_at: datetime | None = None
    payoff: float | None = None
    likelihood: float | None = None
    estimate_minutes: int | None = None


def test_defaults_are_used_and_named():
    t = T(1, area="Coreflow/CLARA")
    # 3 x 0.7 / 0.5 h x 2.2 x 1.0
    assert rules.score(t, NOW) == pytest.approx(9.24)
    why = rules.explain(t, NOW)
    assert "defaults: payoff, likelihood, estimate" in why and why.startswith("score 9.24")


@pytest.mark.parametrize("payoff,likelihood,estimate,area", [
    (9, 3.0, 0, "banana"), (-4, -1, -5, None), ("x", "y", "z", ""), (float("nan"), None, 10**9, "unsorted"),
])
def test_garbage_clamps_never_raises_never_zero(payoff, likelihood, estimate, area):
    t = T(1, area=area, payoff=payoff, likelihood=likelihood, estimate_minutes=estimate)
    s = rules.score(t, NOW)
    assert s > 0 and s == s
    assert rules.explain(t, NOW)
    assert rules.rank([t, T(2)], NOW)             # sortable


def test_clamping_values():
    hi = T(1, payoff=9, likelihood=3.0, estimate_minutes=1)         # -> 5, 1.0, 5 min
    assert rules.score(hi, NOW) == pytest.approx(5 * 1.0 / (5 / 60) * 1.8)
    lo = T(2, area="unsorted", payoff=0.2, likelihood=0.001)         # -> 1, 0.05, unsorted = 5
    assert rules.score(lo, NOW) == pytest.approx(1 * 0.05 / 0.5 * 0.5)


@pytest.mark.parametrize("hours,boost", [
    (-1, 4.0), (0, 4.0), (0.5, 3.0), (24, 3.0), (24.01, 2.0), (48, 2.0),
    (48.01, 1.0 + (168 - 48.01) / 120), (100, 1.0 + 68 / 120), (168, 1.0), (169, 1.0),
])
def test_deadline_boost_curve(hours, boost):
    assert rules.deadline_boost(NOW + timedelta(hours=hours), NOW) == pytest.approx(boost)


def test_boost_continuous_at_48h_and_none_is_one():
    a = rules.deadline_boost(NOW + timedelta(hours=48), NOW)
    b = rules.deadline_boost(NOW + timedelta(hours=48, seconds=1), NOW)
    assert a == pytest.approx(b, abs=1e-3)
    assert rules.deadline_boost(None, NOW) == 1.0


def test_guardrail_2_deadline_inside_48h_beats_any_undated_item():
    star = T(1, area="Coreflow/CLARA", payoff=5, likelihood=1.0, estimate_minutes=5)   # huge score
    dull = T(2, area="unsorted", payoff=1, likelihood=0.05, estimate_minutes=480,
             due_at=NOW + timedelta(hours=47))
    assert rules.score(star, NOW) > 1000 * rules.score(dull, NOW)
    assert [t.id for t in rules.rank([star, dull], NOW)] == [2, 1]
    assert rules.explain(dull, NOW).startswith("URGENT (due in 47h)")
    dull.due_at = NOW + timedelta(hours=49)                  # outside the window: score decides
    assert [t.id for t in rules.rank([star, dull], NOW)] == [1, 2]


def test_overdue_vs_due_soon_both_urgent_so_score_decides():
    overdue = T(1, area="Home & life", due_at=NOW - timedelta(hours=3))     # boost 4, weight 0.5
    soon = T(2, area="Coreflow/CLARA", due_at=NOW + timedelta(hours=1))     # boost 3, weight 2.2
    assert rules.urgent(overdue, NOW) and rules.urgent(soon, NOW)
    assert [t.id for t in rules.rank([overdue, soon], NOW)] == [2, 1]
    soon.area = "Home & life"                                              # same weight now
    assert [t.id for t in rules.rank([overdue, soon], NOW)] == [1, 2]
    assert "URGENT (overdue)" in rules.explain(overdue, NOW)


def test_tie_breaks_due_then_area_then_id():
    # equal scores: undated vs dated > 7 days (boost 1.0 both)
    a = T(5, area="School")
    b = T(6, area="School", due_at=NOW + timedelta(days=20))
    assert [t.id for t in rules.rank([a, b], NOW)] == [6, 5]               # earlier due first
    # equal score, both undated, different area weights but tuned to the same score
    c = T(7, area="Portfolio", payoff=3)                  # 3 x .7 / .5 x 1.2 = 5.04
    d = T(8, area="School", payoff=2)                     # 2 x .7 / .5 x 1.8 = 5.04
    assert rules.score(c, NOW) == pytest.approx(rules.score(d, NOW))
    assert [t.id for t in rules.rank([c, d], NOW)] == [8, 7]               # heavier area first
    e, f = T(10), T(9)
    assert [t.id for t in rules.rank([e, f], NOW)] == [9, 10]              # then oldest id


def fill_three(service):
    return [service.add(f"active {i}", area="School") for i in range(3)]


def test_promotion_by_rank_deadline_in_30h(service, clock):
    clock.set(NOW)
    a = fill_three(service)
    light = service.add("light but due", area="Home & life", due_at=NOW + timedelta(hours=30))
    heavy = service.add("coreflow big", area="Coreflow/CLARA", payoff=5)
    assert light.status == heavy.status == "queued"
    _, promoted = service.mark_done(a[0].id)
    assert [p.id for p in promoted] == [light.id]           # Guardrail 2


def test_promotion_by_rank_deadline_in_10_days(service, clock):
    clock.set(NOW)
    a = fill_three(service)
    light = service.add("light, due later", area="Home & life", due_at=NOW + timedelta(days=10))
    heavy = service.add("coreflow big", area="Coreflow/CLARA", payoff=5)
    _, promoted = service.mark_done(a[0].id)
    assert [p.id for p in promoted] == [heavy.id]
    ev = [r for r in db.events_for(service.conn, heavy.id) if r["kind"] == "promoted"][0]
    done_ev = [r for r in db.events_for(service.conn, a[0].id) if r["kind"] == "done"][0]
    assert ev["cause_id"] == done_ev["id"]                  # promotion points at its cause


def test_morning_post_logs_rank_score_why_for_top_three_and_extras(service, clock):
    clock.set(datetime(2026, 10, 5, 7, 58, tzinfo=ET))
    x = service.add("coreflow", area="Coreflow/CLARA", payoff=5)
    y = service.add("school due tomorrow", area="School", due_at=datetime(2026, 10, 6, 9, 0, tzinfo=ET))
    z = service.add("garden", area="Home & life")
    late = service.add("queued past due", area="Portfolio", status="queued")
    service.conn.execute("UPDATE tasks SET due_at=?, next_nag_at=? WHERE id=?",
                         (db.to_db(NOW - timedelta(days=1)), db.to_db(NOW - timedelta(days=1)), late.id))
    service.conn.commit()
    poster = PrintPoster(clock, out=lambda *_: None)
    for _ in range(4):
        tick(service, poster, clock, RunState())
        clock.advance(timedelta(minutes=1))
    morning = [m for _, m in poster.sent if m.kind == "morning"][0]
    assert [t.id for t in morning.items] == [y.id, x.id, z.id, late.id]   # urgent first, extra last
    rows = service.conn.execute("SELECT task_id, detail, actor FROM events WHERE kind='morning_post' "
                                "ORDER BY id").fetchall()
    details = {r["task_id"]: json.loads(r["detail"]) for r in rows}
    assert [details[i]["rank"] for i in (y.id, x.id, z.id)] == [1, 2, 3]
    assert details[y.id]["why"].startswith("URGENT (due in 25h)")
    assert details[x.id]["score"] > details[z.id]["score"]
    assert details[late.id].get("extra") is True and "rank" not in details[late.id]
    assert {r["actor"] for r in rows} == {"scheduler"}
    assert morning.why[y.id] == details[y.id]["why"]
