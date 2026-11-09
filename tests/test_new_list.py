"""'new list' (2026-10-16): re-rank mid-day without the AI, keep what was just started,
respect today's mode and same-day swaps, and Undo puts the previous three back."""
import asyncio
from datetime import datetime, timedelta

import pytest

from dispatch import chat, legend, rules
from dispatch.clock import ET

NOW = datetime(2026, 10, 16, 14, 0, tzinfo=ET)


async def _inline(fn, *a, **kw):
    return fn(*a, **kw)


def router(service):
    def ask(prompt):
        raise AssertionError("'new list' must not call the local AI")
    return chat.Router(service, ask=ask, guess=lambda t, n: None, run=_inline)


def status(service, tid):
    return service.get(tid).status


@pytest.fixture
def s(service, clock):
    clock.set(NOW)
    ids = {}
    # three weak items take the slots first, then three strong ones queue up
    for key, payoff in [("weak1", 1), ("weak2", 1), ("weak3", 2),
                        ("strong1", 5), ("strong2", 5), ("strong3", 4)]:
        ids[key] = service.add(key, area="Coreflow/CLARA", payoff=payoff, likelihood=0.9,
                               estimate_minutes=30, place="home", energy="light").id
    clock.advance(timedelta(minutes=1))
    return ids


@pytest.mark.parametrize("text", ["new list", "New List", "new list please", "refresh",
                                  "refresh my list", "re-rank", "rerank", "reshuffle",
                                  "new three", "update the list", "new priorities!"])
def test_trigger_words(text):
    assert chat.is_new_list(text)


@pytest.mark.parametrize("text", ["add: new list of vendors", "list", "new", "refresh #3",
                                  "make a new list of fans to buy", ""])
def test_not_trigger(text):
    assert not chat.is_new_list(text)


def test_reranks_and_undo_restores(service, s):
    r = asyncio.run(router(service).handle("new list"))
    assert r.action == "new_list" and r.undo_event_id
    assert {status(service, s[k]) for k in ("strong1", "strong2", "strong3")} == {"active"}
    assert {status(service, s[k]) for k in ("weak1", "weak2", "weak3")} == {"queued"}
    assert all(service.get(s[k]).snooze_count == 0 for k in s)
    assert sorted(r.task_ids) == sorted(s[k] for k in ("strong1", "strong2", "strong3"))
    assert r.buttons and all(cid.startswith("done:") for _, cid in r.buttons)
    assert "Back to the queue" in r.text and "\n1. #" in r.text
    u = service.undo(r.undo_event_id, actor="chat")
    assert "previous three are back" in u.text
    assert {status(service, s[k]) for k in ("weak1", "weak2", "weak3")} == {"active"}
    assert {status(service, s[k]) for k in ("strong1", "strong2", "strong3")} == {"queued"}


def test_undo_that_reverses_it(service, s):
    rt = router(service)
    asyncio.run(rt.handle("new list"))
    assert service.latest_undoable() is not None
    ev = service.conn.execute("SELECT kind FROM events WHERE id=?",
                              (service.latest_undoable(),)).fetchone()[0]
    assert ev == "repick"


def test_already_best_changes_nothing(service, s):
    asyncio.run(router(service).handle("new list"))
    before = [tuple(r) for r in service.conn.execute("SELECT * FROM tasks ORDER BY id")]
    r = asyncio.run(router(service).handle("refresh"))
    assert r.undo_event_id is None and "Same three" in r.text
    assert before == [tuple(x) for x in service.conn.execute("SELECT * FROM tasks ORDER BY id")]


def test_started_item_keeps_its_slot(service, clock, s):
    service.do_now(s["weak1"], actor="button")
    clock.advance(timedelta(minutes=10))
    r = service.new_list(actor="chat")
    assert status(service, s["weak1"]) == "active"
    assert "(started - kept)" in r.text
    assert service.active_count() == rules.ACTIVE_CAP
    # an hour later it is fair game again
    clock.advance(timedelta(hours=1))
    service.new_list(actor="chat")
    assert status(service, s["weak1"]) == "queued"


def test_swapped_out_today_never_comes_back(service, s):
    service.new_list(actor="chat")
    service.set_fields(s["weak1"], actor="chat", energy="focused")
    res = service.swap(s["strong1"], "energy", actor="button")
    assert res.replacement is not None and status(service, s["strong1"]) == "queued"
    service.new_list(actor="chat")
    assert status(service, s["strong1"]) == "queued"


def test_day_mode_filter_applies(service, clock, s):
    service.set_fields(s["strong1"], actor="chat", place="out")
    service.set_mode(rules.DayMode.from_json({"name": "sick", "places": ["home"],
                                             "max_minutes": 45, "light_first": True}),
                     actor="chat")
    clock.advance(timedelta(minutes=1))
    r = service.new_list(actor="chat")
    assert status(service, s["strong1"]) != "active"
    assert "filter on" in r.text


def test_after_finishing_fills_and_reranks(service, clock, s):
    service.mark_done(s["weak1"], actor="button")     # strong item fills the free slot
    clock.advance(timedelta(minutes=5))
    service.new_list(actor="chat")
    assert {status(service, s[k]) for k in ("strong1", "strong2", "strong3")} == {"active"}


def test_legend_mentions_it():
    assert "new list" in legend.help_text()
    assert len(legend.help_text()) <= chat.MAX_REPLY


def test_not_counted_as_didnt_fit(service, s):
    service.new_list(actor="chat")
    assert service.mode_demoted_days(s["weak1"]) == set()
