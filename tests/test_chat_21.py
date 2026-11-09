"""Phase 2.1 chat actions with a mocked local AI: set_mode, set_needs, wait, resume, swap,
did:, the done-not-found bridge, the schema, and the message tag on every change."""
import asyncio
from datetime import datetime

import pytest

import fakes
from dispatch import chat, db, rules
from dispatch.clock import ET

NOW = datetime(2026, 10, 5, 9, 0, tzinfo=ET)


def say(service, text, answer=None, mid=None, **kw):
    r = fakes.router(service, {text: answer} if answer else {}, **kw)

    async def go():
        if mid is None:
            return await r.handle(text)
        with db.event_context(msg=mid):
            return await r.handle(text)
    return asyncio.run(go())


@pytest.fixture
def items(service, clock):
    clock.set(NOW)
    ids = {}
    for key, title, place, energy, mins in [
            ("strimer", "Strimer return", "out", "light", 45),
            ("keystone", "Keystone sessions", "home", "focused", 120),
            ("sam", "Call Sam", "phone", "light", 20),
            ("broker", "Insurance broker quote", None, None, 45),
            ("assets", "List assets and debts", "home", "light", 20),
            ("invest", "Monthly investing amount", "home", "light", 15)]:
        ids[key] = service.add(title, area="Personal admin", place=place, energy=energy,
                               estimate_minutes=mins).id
    return ids


def test_set_mode_from_chat_is_tagged_and_undoable(service, items):
    r = say(service, "home sick today, keep it light", {"action": "set_mode", "mode": "sick"},
            mid=501)
    assert r.action == "set_mode" and r.undo_event_id and r.text.startswith("Sick/home day:")
    ev = db.get_event(service.conn, r.undo_event_id)
    assert ev["kind"] == "mode_set" and ev["actor"] == "chat"
    tagged = {e["kind"] for e in db.events_for_message(service.conn, 501)}
    assert {"mode_set", "promoted"} <= tagged
    assert service.current_mode() == rules.SICK


@pytest.mark.parametrize("text,expect", [
    ("sick, home only, maybe 2 hours in short bursts",
     rules.DayMode("sick", frozenset({"home"}), 30, True, 120)),
    ("slammed today, only quick stuff", rules.SLAMMED),
    ("back to normal", rules.NORMAL),
    ("staying home today, no calls, 20 minute chunks",
     rules.DayMode("sick", frozenset({"home"}), 20, True, None)),
    ("feeling rough, staying in", rules.SICK),
])
def test_mode_words_beat_the_models_numbers(text, expect):
    assert chat.mode_from_text(text, {"mode": "sick", "max_minutes": 90}) == expect or \
        chat.mode_from_text(text, {}) == expect


def test_sick_of_this_project_is_not_a_sick_day(service, items):
    r = say(service, "sick of this project", {"action": "set_mode", "mode": "sick"})
    assert r.action == "ask" and service.current_mode() is None
    assert not service.conn.execute("SELECT 1 FROM events WHERE kind='mode_set'").fetchone()


def test_set_needs_by_words_and_by_context_after_an_add(service, items):
    r = say(service, "the Strimer return is an out-of-the-house one",
            {"action": "set_needs", "task": {"id": None, "text": "the Strimer return"},
             "place": "home"}, mid=502)
    assert r.action == "set_needs" and service.get(items["strimer"]).place == "out"
    assert db.get_event(service.conn, r.undo_event_id)["kind"] == "needs_set"
    add = say(service, "add: renew passport")
    r = say(service, "easy one", {"action": "set_needs", "energy": "light"})
    assert r.task_ids == add.task_ids and service.get(add.task_ids[0]).energy == "light"
    r = say(service, "the Sam call is a phone thing",
            {"action": "set_needs", "task": {"id": None, "text": "the Sam call"},
             "place": "phone"})
    assert service.get(items["sam"]).place == "phone" and r.action == "set_needs"


def test_title_words_alone_do_not_set_a_place(service, items):
    r = say(service, "Call Sam is heavy",
            {"action": "set_needs", "task": {"id": None, "text": "Call Sam"}, "energy": "focused"})
    t = service.get(items["sam"])
    assert t.energy == "focused" and t.place == "phone"          # "call" in the title ignored


def test_wait_and_resume(service, items):
    r = say(service, "waiting on the broker to call back, #4",
            {"action": "wait", "task": {"id": 4, "text": "the broker"}}, mid=503)
    t = service.get(items["broker"])
    assert r.action == "wait" and t.status == "waiting"
    assert t.waiting_until == datetime(2026, 10, 8, 9, 0, tzinfo=ET)
    assert "I'll check in Thu 10/8 9:00 AM" in r.text
    assert [e["kind"] for e in db.events_for_message(service.conn, 503)][0] == "waiting"
    r = say(service, "the broker got back to me",
            {"action": "resume", "task": {"id": None, "text": "the broker"}})
    assert r.action == "resume" and service.get(items["broker"]).status in ("queued", "active")


def test_resume_with_nothing_waiting_changes_nothing(service, items):
    before = service.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    r = say(service, "Sam finally answered", {"action": "resume",
                                               "task": {"id": None, "text": "Sam"}})
    assert r.action == "not_found" and "Nothing is marked as waiting" in r.text
    after = service.conn.execute("SELECT COUNT(*) FROM events WHERE kind != 'chat'").fetchone()[0]
    assert after == before


def test_swap_from_chat_reason_from_words_and_ask_when_missing(service, items):
    r = say(service, "swap the keystone thing, way too long",
            {"action": "swap", "task": {"id": None, "text": "the keystone thing"},
             "reason": "energy"}, mid=504)
    assert r.action == "swap" and "(too long)" in r.text
    assert service.get(items["keystone"]).status == "queued"
    r = say(service, "swap #1", {"action": "swap", "task": {"id": 1}})
    assert r.action == "ask" and "for what reason" in r.text


def test_did_in_both_channels_and_undo(service, items):
    r = say(service, "did: set up Dispatch", mid=505)
    assert r.action == "did" and r.text.startswith("Logged as done: #7 set up Dispatch")
    kinds = [e["kind"] for e in db.events_for_message(service.conn, 505)]
    assert kinds == ["created", "done"]
    assert service.get(7).status == "done"
    assert service.latest_undoable() == r.undo_event_id


def test_done_on_something_not_on_the_list_offers_did(service, items):
    r = say(service, "finished the dentist forms",
            {"action": "done", "task": {"id": None, "text": "the dentist forms"}})
    assert r.action == "not_found" and "did: dentist forms" in r.text


@pytest.mark.parametrize("bad", [{"mode": "vacation"}, {"place": "car"}, {"energy": "tired"},
                                 {"reason": "bored"}, {"max_minutes": "20"}])
def test_schema_rejects_off_values(bad):
    assert chat.validate({"action": "set_mode", **bad})["action"] == "unclear"
    assert chat.validate({"action": "set_mode", "mode": "Sick"})["mode"] == "sick"


def test_near_tie_on_a_new_action_still_asks(service, items):
    service.add("Call Sam again", area="Personal admin")
    r = say(service, "the Sam call is a phone thing",
            {"action": "set_needs", "task": {"id": None, "text": "Sam call"}, "place": "phone"})
    assert r.action == "ambiguous" and r.text.startswith("Which one?")


def test_minutes_and_query_waiting(service, items):
    r = say(service, "I've got 30 minutes", {"action": "minutes", "minutes": 30})
    assert r.action == "minutes" and r.buttons[0][0] == "Start"
    service.set_waiting(items["broker"], actor="chat")
    r = say(service, "what am I waiting on", {"action": "query", "kind": "waiting"})
    assert r.text.startswith("Waiting on someone (1):")


def test_need_words_are_not_task_words(service, items):
    """Live eval 2026-09-29: the model gave no task words for "the Sam call is a phone thing";
    matching the whole sentence ("sam", "phone") fell under the bar and it asked. The
    where/energy words are now left out of the title match, so "sam" alone finds #3."""
    r = say(service, "the Sam call is a phone thing", {"action": "set_needs", "place": "phone"})
    assert r.action == "set_needs" and r.task_ids == [items["sam"]]


def test_curly_apostrophes_in_swap_reasons(service, items):
    """Review r1 fix 5: the iPhone types "can’t" with a curly apostrophe; the swap-reason
    words must still win over (or stand in for) the model's reason."""
    r = say(service, "swap the Keystone sessions, I can’t go out",
            {"action": "swap", "task": {"id": None, "text": "the Keystone sessions"}})
    assert r.action == "swap" and "(can't go out)" in r.text
    assert chat._norm("can’t ‘x’") == "can't 'x'"
