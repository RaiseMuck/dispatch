"""Holds/callbacks (Discord + phone) and the Dispatch-calls-Max engine (2026-10-18)."""
import asyncio
import json
from datetime import datetime, timedelta

import pytest

from dispatch import calls, chat, holds, voice
from dispatch.clock import ET

NOW = datetime(2026, 10, 19, 15, 0, tzinfo=ET)        # Monday 3 PM
REAL_BLOCKS = {   # a made-up week for the busy-block tests
    0: [("10:00", "11:30", "seminar")],
    1: [("09:00", "12:30", "class")],
    2: [("08:00", "12:00", "work"), ("13:30", "15:00", "class")],
    3: [("09:00", "12:30", "class"), ("16:00", "17:00", "lab")],
    4: [("10:00", "16:00", "work")],
}


@pytest.fixture(autouse=True)
def _no_busy_blocks(monkeypatch):
    """The older tests here predate class/work hours; the busy-block tests put them back."""
    monkeypatch.setattr(calls, "BUSY_BLOCKS", {})
MAX = "+16145550123"


async def _inline(fn, *a, **kw):
    return fn(*a, **kw)


class FakeVapi:
    def __init__(self, spend=0.0):
        self.posts, self.spend = [], spend

    def call(self, method, path, body=None):
        if method == "GET":
            return [{"cost": self.spend}]
        self.posts.append((path, body))
        return {"id": "call-1"}


def link_for(service, spend=0.0):
    return voice.VoiceLink(service, None, FakeVapi(spend), "asst", "4821", MAX, run=_inline)


@pytest.mark.parametrize("text,delta", [("no calls for 2 hours", timedelta(hours=2)),
                                        ("I'm busy for 30 minutes", timedelta(minutes=30)),
                                        ("no calls for an hour", timedelta(hours=1))])
def test_hold_for(service, clock, text, delta):
    clock.set(NOW)
    r = holds.command(service, text, NOW)
    assert r.startswith("No calls until") and holds.hold_until(service, NOW) == NOW + delta


def test_hold_until_and_today_and_resume(service, clock):
    assert "5:00 PM" in holds.command(service, "no calls until 5pm", NOW)
    holds.command(service, "no calls today", NOW)
    assert holds.hold_until(service, NOW).hour == 0
    assert holds.command(service, "calls on", NOW) == "Calls are back on."
    assert holds.hold_until(service, NOW) is None


def test_call_me(service):
    assert "3:20 PM" in holds.command(service, "call me in 20 minutes", NOW)
    assert "4:00 PM" in holds.command(service, "call me at 4pm", NOW)


@pytest.mark.parametrize("text", ["call the bank", "no", "calls are expensive", "add: no calls"])
def test_not_hold_commands(service, text):
    assert holds.command(service, text, NOW) is None


def test_discord_no_calls_never_asks_ai(service, clock):
    clock.set(NOW)
    def ask(prompt):
        raise AssertionError("must not call the AI")
    r = asyncio.run(chat.Router(service, ask=ask, guess=lambda t, n: None, run=_inline)
                    .handle("no calls until 6pm"))
    assert r.action == "calls" and "6:00 PM" in r.text


def test_engine_off_without_outbound_number(service, clock):
    clock.set(NOW)
    holds.set_callback(service, NOW - timedelta(minutes=1), "test")
    assert calls.tick(service, link_for(service), "", NOW, 13) == []


def test_callback_rings(service, clock):
    clock.set(NOW)
    link = link_for(service)
    holds.set_callback(service, NOW - timedelta(minutes=1), "you asked me to call you back")
    calls.tick(service, link, "num-1", NOW, 13)
    assert len(link.vapi.posts) == 1
    body = link.vapi.posts[0][1]
    assert body["customer"]["number"] == MAX and body["phoneNumberId"] == "num-1"
    assert "4821" not in body["assistantOverrides"]["model"]["messages"][0]["content"]
    assert holds.callback(service) is None


def test_callback_respects_hold(service, clock):
    clock.set(NOW)
    link = link_for(service)
    holds.set_callback(service, NOW - timedelta(minutes=1), "x")
    holds.set_hold(service, NOW + timedelta(hours=1))
    msgs = calls.tick(service, link, "num-1", NOW, 13)
    assert link.vapi.posts == [] and "paused" in msgs[0]


def test_budget_stops_calls(service, clock):
    clock.set(NOW)
    link = link_for(service, spend=13.5)
    holds.set_callback(service, NOW - timedelta(minutes=1), "x")
    msgs = calls.tick(service, link, "num-1", NOW, 13)
    assert link.vapi.posts == [] and "budget" in msgs[0]


def test_stalled_urgent_item_warns_then_calls(service, clock):
    clock.set(NOW - timedelta(hours=5))
    t = service.add("Send the memo", area="School", due_at=NOW + timedelta(hours=6))
    clock.set(NOW)
    link = link_for(service)
    msgs = calls.tick(service, link, "num-1", NOW, 13)
    assert "Heads up" in msgs[0] and link.vapi.posts == []
    assert calls.tick(service, link, "num-1", NOW + timedelta(minutes=2), 13) == []
    calls.tick(service, link, "num-1", NOW + timedelta(minutes=6), 13)
    assert len(link.vapi.posts) == 1
    for m in range(10, 120, 10):                     # never twice about the same item today
        calls.tick(service, link, "num-1", NOW + timedelta(minutes=m), 13)
    assert len(link.vapi.posts) == 1


def test_no_call_if_he_got_moving_during_heads_up(service, clock):
    clock.set(NOW - timedelta(hours=5))
    t = service.add("Send the memo", area="School", due_at=NOW + timedelta(hours=6))
    clock.set(NOW)
    link = link_for(service)
    calls.tick(service, link, "num-1", NOW, 13)
    clock.set(NOW + timedelta(minutes=2))
    service.do_now(t.id, actor="button")
    calls.tick(service, link, "num-1", NOW + timedelta(minutes=6), 13)
    assert link.vapi.posts == []


def test_no_accountability_calls_before_10am(service, clock):
    early = NOW.replace(hour=9)
    clock.set(early - timedelta(hours=5))
    service.add("Send the memo", area="School", due_at=early + timedelta(hours=2))
    clock.set(early)
    assert calls.tick(service, link_for(service), "num-1", early, 13) == []


# --- class and work hours (2026-10-28) ---
TUE_CLASS = datetime(2026, 10, 27, 10, 0, tzinfo=ET)     # Tuesday mid-morning class
FRI_WORK = datetime(2026, 10, 30, 11, 0, tzinfo=ET)      # Friday 11 AM, working from home


@pytest.mark.parametrize("at,busy", [
    (TUE_CLASS, True), (FRI_WORK, True),
    (datetime(2026, 10, 27, 8, 55, tzinfo=ET), True),    # 5 min before class: inside the pad
    (datetime(2026, 10, 27, 11, 0, tzinfo=ET), True),    # mid block
    (datetime(2026, 10, 27, 12, 35, tzinfo=ET), False),  # Tuesday classes over
    (datetime(2026, 10, 28, 12, 45, tzinfo=ET), False),  # Wednesday between work and class
    (datetime(2026, 10, 29, 16, 30, tzinfo=ET), True),   # Thursday lab
    (datetime(2026, 10, 31, 12, 0, tzinfo=ET), False),   # Saturday
])
def test_busy_blocks(monkeypatch, at, busy):
    monkeypatch.setattr(calls, "BUSY_BLOCKS", REAL_BLOCKS)
    assert (calls.busy_until(at) is not None) == busy


def test_no_accountability_call_during_class(service, clock, monkeypatch):
    monkeypatch.setattr(calls, "BUSY_BLOCKS", REAL_BLOCKS)
    clock.set(TUE_CLASS - timedelta(hours=5))
    service.add("Send the memo", area="School", due_at=TUE_CLASS + timedelta(hours=6))
    clock.set(TUE_CLASS)
    link = link_for(service)
    assert calls.tick(service, link, "num-1", TUE_CLASS, 13) == [] and link.vapi.posts == []


def test_no_heads_up_for_a_call_that_would_land_in_class(service, clock, monkeypatch):
    monkeypatch.setattr(calls, "BUSY_BLOCKS", REAL_BLOCKS)
    before = datetime(2026, 10, 27, 10, 57, tzinfo=ET)    # call would ring 11:02, inside the pad
    clock.set(before - timedelta(hours=5))
    service.add("Send the memo", area="School", due_at=before + timedelta(hours=6))
    clock.set(before)
    assert calls.tick(service, link_for(service), "num-1", before, 13) == []


def test_callback_during_work_waits_until_after(service, clock, monkeypatch):
    monkeypatch.setattr(calls, "BUSY_BLOCKS", REAL_BLOCKS)
    clock.set(FRI_WORK)
    link = link_for(service)
    holds.set_callback(service, FRI_WORK - timedelta(minutes=1), "you asked me to call you back")
    msgs = calls.tick(service, link, "num-1", FRI_WORK, 13)
    assert link.vapi.posts == [] and "4:00 PM" in msgs[0]
    after = FRI_WORK.replace(hour=16, minute=1)
    clock.set(after)
    calls.tick(service, link, "num-1", after, 13)
    assert len(link.vapi.posts) == 1


def test_no_more_checkin_calls_after_an_unanswered_one(service, clock):
    clock.set(NOW - timedelta(hours=5))
    service.add("Send the memo", area="School", due_at=NOW + timedelta(hours=6))
    clock.set(NOW)
    service.meta_set(voice.UNANSWERED_KEY + NOW.date().isoformat(), "1")
    link = link_for(service)
    for m in range(0, 120, 3):
        assert calls.tick(service, link, "num-1", NOW + timedelta(minutes=m), 13) == []
    assert link.vapi.posts == []


def test_callback_still_rings_after_an_unanswered_call(service, clock):
    service.meta_set(voice.UNANSWERED_KEY + NOW.date().isoformat(), "1")
    holds.set_callback(service, NOW - timedelta(minutes=1), "you asked for a 3 PM call")
    link = link_for(service)
    calls.tick(service, link, "num-1", NOW, 13)
    assert len(link.vapi.posts) == 1


# ---- 2026-11-02: what a call says, and what it's about ----------------------------------------
def test_call_leaves_a_real_briefing_at_the_beep(service, clock):
    clock.set(NOW - timedelta(hours=5))
    hw = service.add("Submit BF Homework #1 with team", area="School", due_at=NOW.replace(hour=21),
                     notes="You're in no Carmen group, so a teammate's upload won't count. Check Carmen.")
    service.add("Pay the phone bill", area="Personal admin", due_at=NOW.replace(hour=23))
    service.add("Ask Priya after class", area="School", due_at=NOW + timedelta(days=1))
    clock.set(NOW)
    link = link_for(service)
    calls.tick(service, link, "num-1", NOW, 13)
    calls.tick(service, link, "num-1", NOW + timedelta(minutes=6), 13)
    (path, body), = link.vapi.posts
    o = body["assistantOverrides"]
    assert o["voicemailDetection"]["provider"] == "vapi"
    vm = o["voicemailMessage"]
    assert vm.startswith("Max, it's Augustine.") and "First: Submit Behavioral Finance Homework number 1 with team, due at" in vm
    assert "Carmen group" in vm and "1 more thing is due today" in vm and "Ask Priya" in vm
    assert "hasn't moved" not in vm and "#" not in vm
    assert len(vm.split()) <= calls.VOICEMAIL_WORDS
    assert "Plus 1 more due today" in o["firstMessage"]
    assert json.loads(service.meta_get(voice.BRIEF_KEY))["text"].startswith(f"**#{hw.id} Submit BF")


def test_a_deadline_still_ahead_beats_a_stale_overdue_pick(service, clock):
    clock.set(NOW - timedelta(days=4))
    stale = service.add("Decide on the 2 AM calls", area="Coreflow/CLARA", due_at=NOW - timedelta(days=3))
    clock.set(NOW - timedelta(hours=5))
    hw = service.add("Submit the homework", area="School", due_at=NOW + timedelta(hours=6))
    clock.set(NOW)
    assert calls.call_subject(service, NOW, calls._day_log(service, NOW)).id == hw.id
    # an item called about on each of the last two days rests until he touches it
    for i in (1, 2):
        d = (NOW - timedelta(days=i)).date().isoformat()
        service.meta_set(calls.LOG_KEY + d, json.dumps({"count": 1, "items": [hw.id], "dead": False,
                                                        "budget_note": False}))
    assert calls.call_subject(service, NOW, calls._day_log(service, NOW)).id == stale.id
