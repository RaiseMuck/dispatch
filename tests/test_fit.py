"""Phase 2.1: which items fit today's mode (pure rules)."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from dispatch import rules
from dispatch.clock import ET

NOW = datetime(2026, 10, 5, 9, 0, tzinfo=ET)


def item(id=1, place=None, energy=None, minutes=None, due=None, payoff=3, area="School"):
    return SimpleNamespace(id=id, place=place, energy=energy, estimate_minutes=minutes,
                           due_at=due, payoff=payoff, likelihood=None, area=area)


@pytest.mark.parametrize("place", rules.PLACES)
@pytest.mark.parametrize("energy", rules.ENERGIES)
@pytest.mark.parametrize("minutes", [5, 20, 21, 45, 46, 120])
def test_fits_every_combination(place, energy, minutes):
    t = item(place=place, energy=energy, minutes=minutes)
    assert rules.fits(t, None) and rules.fits(t, rules.NORMAL)
    assert rules.fits(t, rules.SICK) == (place == "home" and minutes <= 45)
    assert rules.fits(t, rules.SLAMMED) == (place in ("home", "phone") and minutes <= 20)


def test_unknown_needs_count_as_home_light_30():
    t = item()
    assert rules.needs(t) == ("home", "light", 30)
    assert rules.fits(t, rules.SICK) and not rules.fits(t, rules.SLAMMED)
    assert rules.needs(item(place="couch", energy="tired", minutes=0)) == ("home", "light", 30)


def test_light_first_only_when_the_mode_asks():
    heavy = item(1, energy="focused", minutes=10, payoff=5)      # better score
    easy = item(2, energy="light", minutes=30, payoff=2)
    assert [t.id for t in rules.fit_rank([heavy, easy], rules.SICK, NOW)] == [2, 1]
    assert [t.id for t in rules.fit_rank([heavy, easy], rules.SLAMMED, NOW)] == [1]
    assert [t.id for t in rules.fit_rank([heavy, easy], None, NOW)] == [1, 2]
    # a sick day with only focused home items still fills from them (energy is no filter)
    assert [t.id for t in rules.fit_rank([heavy], rules.SICK, NOW)] == [1]


def test_urgent_still_first_within_the_fit_set():
    soon = item(1, energy="light", minutes=40, payoff=1, due=NOW + timedelta(hours=20))
    good = item(2, energy="light", minutes=10, payoff=5)
    out = item(3, place="out", minutes=10, due=NOW - timedelta(days=1))
    assert [t.id for t in rules.fit_rank([good, soon, out], rules.SICK, NOW)] == [1, 2]


def test_why_not_wording():
    assert rules.why_not(item(place="out", minutes=90), rules.SICK) == \
        "needs going out, ~1.5 h > 45 min"
    assert rules.why_not(item(place="phone"), rules.SICK) == "is a phone call"
    assert rules.why_not(item(minutes=30), rules.SLAMMED) == "~30 min > 20 min"
    assert rules.why_not(item(place="out"), None) == ""


def test_mode_json_round_trip_and_words():
    m = rules.DayMode("sick", frozenset({"home"}), 30, True, 120)
    assert rules.DayMode.from_json(m.to_json()) == m
    assert rules.SICK.describe() == "home only, light stuff first, items <= 45 min"
    assert rules.SLAMMED.describe() == "home or calls only, items <= 20 min"
    assert m.describe().endswith("~2 h total")


def test_new_button_sets():
    assert rules.waiting_options(0) == ["gotit", "waitmore", "kill"]
    assert rules.waiting_options(2) == ["gotit", "pickday", "kill"]
    assert rules.flagged_options() == ["donow", "pickday", "kill"]
    assert rules.snooze_options(0) == ["done", "plus2h", "tonight", "tomorrow", "kill"]  # unchanged


# ---- Phase 2.1 review r1 should-fixes 1 and 3 ------------------------------------------------
def test_a_deadline_beats_light_first(service, clock):
    """Should-fix 1: the Guardrail 2 tier stays outermost. On a sick day a fitting FOCUSED item
    due in 3 hours takes a slot before light undated items."""
    clock.set(NOW)
    for i in range(3):
        service.add(f"out {i}", place="out")                         # the three, none fit
    light = [service.add(f"light {i}", place="home", energy="light", estimate_minutes=10,
                         payoff=5) for i in range(3)]
    due = service.add("focused, due soon", place="home", energy="focused", estimate_minutes=40,
                      due_at=NOW + timedelta(hours=3))
    assert rules.fit_key(due, rules.SICK, NOW) < rules.fit_key(light[0], rules.SICK, NOW)
    service.set_mode(rules.SICK, actor="chat")
    active = {t.id for t in service.by_status("active")}
    assert due.id in active and len(active) == 3


def test_unknown_place_uses_title_hints_before_the_default():
    """Should-fix 3: before the local AI labels an item, a title that clearly means going out
    or calling must not count as fitting a home-only day."""
    strimer = SimpleNamespace(id=1, title="Return the Strimer Wireless at Micro Center",
                              place=None, energy=None, estimate_minutes=None)
    sam = SimpleNamespace(id=2, title="Call Sam at Acme Plumbing", place=None, energy=None,
                           estimate_minutes=None)
    memo = SimpleNamespace(id=3, title="Draft the 1099 memo", place=None, energy=None,
                           estimate_minutes=None)
    pick = SimpleNamespace(id=4, title="Pick up the dry cleaning", place=None, energy=None,
                           estimate_minutes=None)
    assert [rules.needs(t)[0] for t in (strimer, sam, memo, pick)] == \
        ["out", "phone", "home", "out"]
    assert not rules.fits(strimer, rules.SICK) and not rules.fits(sam, rules.SICK)
    assert rules.fits(memo, rules.SICK)
    labelled = SimpleNamespace(id=5, title="Call Sam", place="home", energy=None,
                               estimate_minutes=None)
    assert rules.needs(labelled)[0] == "home"                       # a known place always wins


def test_title_hints_measured_on_the_live_items():
    """From the live-item measurement: "external USB drive" is not an errand; driving to a
    place is."""
    usb = SimpleNamespace(title="Give the MacBook an off-machine backup (external USB drive)")
    trip = SimpleNamespace(title="drive to Mom's to drop off the garden stakes")
    fans = SimpleNamespace(title="return 6 fans to Newegg")
    assert rules.place_hint(usb.title) is None
    assert rules.place_hint(trip.title) == "out" and rules.place_hint(fans.title) == "out"
