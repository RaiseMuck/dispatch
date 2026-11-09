"""Phase 2.1 "I've got N minutes" / "what can I do from bed": one best fit, the three untouched
unless Max taps Start; Skip offers the next; today's mode caps it."""
import asyncio
import json
from datetime import datetime

import pytest

import fakes
from dispatch import rules
from dispatch.clock import ET

NOW = datetime(2026, 10, 5, 11, 30, tzinfo=ET)


def rows(service):
    return [tuple(r) for r in service.conn.execute("SELECT * FROM tasks ORDER BY id")]


@pytest.fixture
def s(service, clock):
    clock.set(NOW)
    ids = {}
    for key, place, energy, mins in [("a_long", "home", "focused", 90), ("a_call", "phone", "light", 25),
                                     ("a_out", "out", "light", 20),
                                     ("q_short", "home", "light", 10),
                                     ("q_focus", "home", "focused", 15)]:
        ids[key] = service.add(key, area="Personal admin", place=place, energy=energy,
                               estimate_minutes=mins).id
    return ids


def ask(service, text, answer):
    r = fakes.router(service, {text: answer})
    return r, asyncio.run(r.handle(text))


def test_best_fit_prefers_one_of_the_three_and_changes_nothing(service, s):
    before = rows(service)
    r, reply = ask(service, "I've got 30 minutes", {"action": "minutes", "minutes": 30})
    assert rows(service) == before                          # the three are untouched
    assert reply.action == "minutes" and reply.undo_event_id is None
    assert reply.text.startswith("Best fit for 30 min: #%d" % s["a_out"]) or \
        reply.text.startswith("Best fit for 30 min: #%d" % s["a_call"])
    assert reply.buttons == [("Start", f"start:{reply.task_ids[0]}"),
                             ("Skip", f"skip:{reply.task_ids[0]}")]


def test_from_bed_is_home_and_light_first(service, s):
    _, reply = ask(service, "what can I do from bed", {"action": "minutes"})
    assert reply.task_ids == [s["q_short"]]                  # no active item is home
    assert "(~10 min, home, light)" in reply.text


def test_the_day_mode_caps_the_minutes(service, s):
    service.set_mode(rules.SLAMMED, actor="chat")
    r, reply = ask(service, "I've got an hour", {"action": "minutes", "minutes": 60})
    t = service.get(reply.task_ids[0])
    assert rules.needs(t)[2] <= 20 and "(slammed day: items up to 20 min.)" in reply.text


def test_skip_offers_the_next_best_and_remembers(service, s):
    r, reply = ask(service, "I've got 30 minutes", {"action": "minutes", "minutes": 30})
    first = reply.task_ids[0]
    nxt = r.skip_reply(first)
    assert nxt.task_ids and nxt.task_ids[0] != first
    assert json.loads(service.meta_get("minutes_query"))["skipped"] == [first]


def test_nothing_fits_names_the_shortest(service, s):
    _, reply = ask(service, "only got 5 minutes", {"action": "minutes", "minutes": 5})
    assert reply.text.startswith("Nothing on the list fits in 5 minutes. Shortest is #%d"
                                 % s["q_short"])
    assert reply.buttons == [("Start", f"start:{s['q_short']}")]


def test_start_on_a_queued_pick_swaps_it_in_and_undo_restores(service, s):
    before = rows(service)
    r, _ = ask(service, "what can I do from bed", {"action": "minutes"})
    res = r.start_reply(s["q_short"])
    assert service.get(s["q_short"]).status == "active" and service.active_count() == 3
    assert "goes back to the queue for now" in res.text and res.undo_event_id
    service.undo(res.undo_event_id, actor="button")
    assert rows(service) == before


def test_quarter_hour_and_hour_and_a_half():
    from dispatch.chat import parse_minutes
    assert parse_minutes("only got a quarter hour") == 15
    assert parse_minutes("an hour and a half") == 90
