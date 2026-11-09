"""Phone line (2026-10-18): push only on change, apply calls only from Max's number, exactly
once, through the normal chat router (so Undo works), retry an unreadable call, never leak
notes or the code."""
import asyncio
import json
from datetime import datetime, timedelta

import pytest

from dispatch import chat, qwen, voice
from dispatch.clock import ET

NOW = datetime(2026, 10, 18, 19, 0, tzinfo=ET)
MAX = "+16145550123"
GUESS = qwen.Guess(area="Personal admin", payoff=3, likelihood=0.7, estimate_minutes=20,
                   guessed=["area"], model="fake", ms=1)


async def _inline(fn, *a, **kw):
    return fn(*a, **kw)


class FakeVapi:
    def __init__(self):
        self.calls = []        # what the line returns for GET /call
        self.sent = []

    def call(self, method, path, body=None):
        self.sent.append((method, path, body))
        if method == "GET":
            return self.calls
        return {"id": "ok"}


def make(service, extract_answer, chat_answers=None):
    chat_answers = chat_answers or {}

    def ask(prompt):
        msg = prompt.rsplit('Message: "', 1)[1].rsplit('"', 1)[0]
        return chat_answers.get(msg, {"action": "unclear"})

    router = chat.Router(service, ask=ask, guess=lambda t, n: GUESS, run=_inline)
    vapi = FakeVapi()
    seen_prompts = []

    def extract(prompt):
        seen_prompts.append(prompt)
        return extract_answer() if callable(extract_answer) else extract_answer

    link = voice.VoiceLink(service, router, vapi, "asst-1", "4821", MAX,
                           extract=extract, run=_inline)
    return link, vapi, seen_prompts


def a_call(cid, number=MAX, status="ended", text="User: done with thirty\nAI: Got it."):
    return {"id": cid, "status": status, "createdAt": "2026-10-18T22:50:00.000Z",
            "startedAt": "2026-10-18T22:50:00.000Z", "customer": {"number": number},
            "artifact": {"transcript": text}}


@pytest.fixture
def items(service, clock):
    clock.set(NOW - timedelta(hours=1))
    a = service.add("Text Sam about the website", area="Coreflow/CLARA",
                    notes="Draft: hey Sam, free Thursday?", estimate_minutes=5)
    b = service.add("Return the fans", area="Home & life", estimate_minutes=30)
    clock.set(NOW)
    return a, b


def run(coro):
    return asyncio.run(coro)


def test_push_only_when_list_changes(service, items):
    link, vapi, _ = make(service, {"commands": []})
    assert run(link.push(NOW)) is True
    assert run(link.push(NOW)) is False
    service.add("New thing", area="Personal admin")
    assert run(link.push(NOW)) is True
    patches = [s for s in vapi.sent if s[0] == "PATCH"]
    assert len(patches) == 2 and patches[0][1] == "/assistant/asst-1"


def test_payload_has_notes_and_keeps_code_out_of_greeting(service, items):
    link, vapi, _ = make(service, {"commands": []})
    run(link.push(NOW))
    body = [s for s in vapi.sent if s[0] == "PATCH"][0][2]
    prompt = body["model"]["messages"][0]["content"]
    assert f"#{items[0].id} Text Sam" in prompt and "hey Sam, free Thursday" in prompt
    assert "THE NEXT 7 DAYS" in prompt and "chief of staff" in prompt
    assert "4821" in prompt and "4821" not in body["firstMessage"]
    assert body["artifactPlan"]["recordingEnabled"] is False


def test_call_from_max_applies_through_router_once(service, items):
    link, vapi, prompts = make(service, {"commands": ["add: call the bank tomorrow 3pm"]})
    vapi.calls = [a_call("c1")]
    res = run(link.pull(NOW))
    assert len(res) == 1 and res[0].commands == ["add: call the bank tomorrow 3pm"]
    _, reply = res[0].replies[0]
    assert reply.action == "add" and reply.undo_event_id
    titles = [t.title for t in service.open_tasks()]
    assert "call the bank" in titles
    assert run(link.pull(NOW)) == []                       # never applied twice
    assert [t.title for t in service.open_tasks()].count("call the bank") == 1
    assert "From your call" in voice.render(res[0])


def test_done_by_number_uses_normal_chat(service, items):
    a, _ = items
    link, vapi, _ = make(service, {"commands": [f"done #{a.id}"]},
                         {f"done #{a.id}": {"action": "done", "task": {"id": a.id, "text": None}}})
    vapi.calls = [a_call("c2", text=f"User: I texted Sam\nAI: Got it: number {a.id}, the Sam text, done.")]
    res = run(link.pull(NOW))
    assert service.get(a.id).status == "done"
    assert res[0].replies[0][1].undo_event_id


def test_other_number_changes_nothing(service, items):
    link, vapi, prompts = make(service, {"commands": ["add: evil"]})
    vapi.calls = [a_call("c3", number="+13125559999")]
    before = len(service.open_tasks())
    res = run(link.pull(NOW))
    assert res[0].note == "other" and res[0].caller_tail == "9999"
    assert len(service.open_tasks()) == before and prompts == []
    assert run(link.pull(NOW)) == []


def test_call_in_progress_waits(service, items):
    link, vapi, _ = make(service, {"commands": []})
    vapi.calls = [a_call("c4", status="in-progress")]
    assert run(link.pull(NOW)) == []
    vapi.calls = [a_call("c4")]
    res = run(link.pull(NOW))
    assert len(res) == 1 and "no changes" in voice.render(res[0])


def test_unreadable_call_retried_then_reported(service, items):
    link, vapi, prompts = make(service, None)
    vapi.calls = [a_call("c5", text="User: move the fans to friday\nAI: Got it.")]
    for _ in range(voice.MAX_TRIES - 1):
        assert run(link.pull(NOW)) == []
    res = run(link.pull(NOW))
    assert res[0].note == "unread" and "move the fans to friday" in voice.render(res[0])
    assert len(prompts) == voice.MAX_TRIES
    assert run(link.pull(NOW)) == []


def test_empty_call(service, items):
    link, vapi, _ = make(service, {"commands": ["add: x"]})
    vapi.calls = [a_call("c6", text="AI: Dispatch. What's your code?")]
    res = run(link.pull(NOW))
    assert res[0].note == "empty" and len(service.open_tasks()) == 2


@pytest.mark.parametrize("obj,want", [
    ({"commands": ["done #3", "rm -rf /", 7, "add: milk", "x" * 300]}, ["done #3", "add: milk"]),
    ({"commands": []}, []),
    ({"nope": 1}, None),
    (None, None),
    ({"commands": ["Sam got back to me on #3", "waiting on Sam for #3", "new list"]},
     ["Sam got back to me on #3", "waiting on Sam for #3", "new list"]),
])
def test_parse_commands(obj, want):
    assert voice.parse_commands(obj) == want


@pytest.mark.parametrize("a,b,ok", [("+16145550123", "6145550123", True),
                                    ("(614) 555-0123", "+1 614 555 0123", True),
                                    ("+16145550124", MAX, False), (None, MAX, False),
                                    ("anonymous", MAX, False)])
def test_same_number(a, b, ok):
    assert voice.same_number(a, b) is ok


def test_messages_transcript_fallback():
    c = {"artifact": {"messages": [{"role": "bot", "message": "Dispatch."},
                                   {"role": "user", "message": "kill number 5"}]}}
    t = voice.transcript_text(c)
    assert voice.max_lines(t) == ["kill number 5"]


def test_settings_feature_flag(monkeypatch):
    from dispatch.config import Settings
    s = Settings(vapi_api_key="KEY-9f3e", vapi_assistant_id="a", voice_pin="4821", max_phone=MAX)
    r = repr(s)
    assert s.features["voice"] is True and "KEY-9f3e" not in r and MAX not in r and "4821" not in r
    assert Settings(vapi_api_key="k").features["voice"] is False


def test_apply_routes_calls_dump_and_errand(service, items):
    from dispatch import errands, holds
    link, vapi, _ = make(service, {"commands": [
        "dump: I need to call the bank, and the fans, and think about Acme Plumbing",
        "add: call the bank tomorrow 3pm",
        "no calls for 2 hours",
        "call me in 20 minutes",
        "errand: email me the Costco memo"]})
    vapi.calls = [a_call("c9")]
    res = run(link.pull(NOW))[0]
    texts = [r.text for _, r in res.replies]
    parked = [t for t in service.by_status("parked") if t.title.startswith("Brain dump")]
    assert len(parked) == 1 and "Acme Plumbing" in parked[0].notes
    assert "call the bank" in [t.title for t in service.open_tasks()]
    assert holds.hold_until(service, NOW) is not None
    assert holds.callback(service) is not None
    jobs = errands.errands(service)
    assert jobs[-1]["status"] == "queued" and "Costco" in jobs[-1]["instruction"]
    assert "NOT run" in texts[-1]


def test_outbound_prompt_skips_code(service, items):
    p = voice.build_prompt("x", "4821", NOW, outbound="number 3 is overdue.")
    assert "4821" not in p and "CALL YOU MADE TO MAX" in p and "number 3 is overdue" in p


def test_gate_drops_numbers_the_call_never_confirmed(service, items):
    a, b = items
    link, vapi, _ = make(service, {"commands": [f"done #{a.id}", f"done #{b.id}", "add: buy fish food"]},
                         {f"done #{a.id}": {"action": "done", "task": {"id": a.id, "text": None}},
                          f"done #{b.id}": {"action": "done", "task": {"id": b.id, "text": None}}})
    vapi.calls = [a_call("c10", text=f"User: brain dump stuff\nAI: Got it: adding buy fish food. "
                                     f"Got it: number {a.id}, the Sam text, done.")]
    res = run(link.pull(NOW))[0]
    assert service.get(a.id).status == "done"
    assert service.get(b.id).status != "done"
    assert res.dropped == [f"done #{b.id}"] and "Skipped" in voice.render(res)


def test_confirmed_numbers():
    t = "AI: Got it: number 34, the Globex review, done. Got it: number 7 moved to Friday.\nUser: number 9 too\nAI: Number 12 is due Friday."
    assert voice.confirmed_numbers(t) == {34, 7}


# ---- 2026-10-30 fixes from the call logs ----------------------------------------------------
def test_confirmed_numbers_joins_spoken_digits():
    # how the line's transcript actually wrote 106, 112 and 158 on 10-25 and 10-30
    t = ("AI: Got it. Number 1 0 6. Fill out the OSU form postponed to tomorrow.\n"
         "AI: Got it. That includes number 1 12. The Monday batch.\n"
         "AI: Got it. Number 1 58, ask the 1 0 0 5 7 3 recruiter, killed. Number 100 is next.")
    assert voice.confirmed_numbers(t) == {106, 112, 158, 100}


def test_payload_lets_him_interrupt():
    body = voice.assistant_payload("x")
    plan = body["stopSpeakingPlan"]
    assert plan["numWords"] == 1 and "stop" in plan["interruptionPhrases"]
    assert "augustine" in plan["interruptionPhrases"]
    assert body["firstMessageInterruptionsEnabled"] is True
    assert 0 < body["model"]["maxTokens"] <= 200


def test_prompt_keeps_turns_short():
    p = voice.build_prompt("x", "4821", NOW)
    assert "One or two short sentences per turn" in p and "Never read more than 3 items" in p
    o = voice.build_prompt("x", "4821", NOW, outbound="number 3 is overdue.")
    assert 'Do NOT say "You\'re in"' in o


def test_unanswered_outbound_call_is_flagged(service, items):
    link, vapi, _ = make(service, {"commands": []})
    c = a_call("c-out", text="AI: Hey Max, it's Augustine. Number 3 is overdue.")
    c["type"] = "outboundPhoneCall"
    vapi.calls = [c]
    res = run(link.pull(NOW))[0]
    assert res.note == "empty" and res.outbound and "didn't pick up" in voice.render(res)
    assert service.meta_get(voice.UNANSWERED_KEY + "2026-10-18")


# ---- 2026-11-02 fixes from the call logs ----------------------------------------------------
def _outbound(cid, text, ended="silence-timed-out"):
    c = a_call(cid, text=text)
    c["type"], c["endedReason"] = "outboundPhoneCall", ended
    return c


def test_voicemail_greeting_is_not_max(service, items):
    # 11-01/11-02: the carrier greeting arrived as "User:" lines, so a missed call looked answered
    link, vapi, prompts = make(service, {"commands": []})
    service.meta_set(voice.BRIEF_KEY, json.dumps({"at": "2026-10-18T18:50:00-04:00",
                                                  "text": "**#3 Send the memo** - due at 9 PM tonight"}))
    vapi.calls = [_outbound("c-vm", "User: Your call has been forwarded to an automated voice messaging system.\n"
                                    "AI: Hey, Max.\nUser: 6 1 4 2 0 2. Is not available.\n"
                                    "User: The mailbox is full and cannot accept any messages at this time. Goodbye.",
                            ended="customer-ended-call")]
    res = run(link.pull(NOW))[0]
    assert res.note == "empty" and prompts == []
    assert service.meta_get(voice.UNANSWERED_KEY + "2026-10-18")
    text = voice.render(res)
    assert "voicemail is full" in text and "Send the memo" in text


def test_detected_voicemail_is_unanswered(service, items):
    link, vapi, _ = make(service, {"commands": []})
    vapi.calls = [_outbound("c-vm2", "AI: Max, it's Augustine.\nUser: Hello?", ended="voicemail")]
    assert run(link.pull(NOW))[0].note == "empty"
    assert service.meta_get(voice.UNANSWERED_KEY + "2026-10-18")


def test_confirmed_numbers_glued_by_the_transcriber():
    # 10-31: 138 came back as "1 to 38", 125 as "1 or 25", 101 as "1 on o 1"
    t = ("AI: Got it. Moving all today's tasks except number 1 to 38.\n"
         "AI: Got it: number 1 or 25, done. Got it: number 1 on o 1, killed.")
    assert {138, 125, 101} <= voice.confirmed_numbers(t)


def test_move_everything_due_today_except(service, clock):
    clock.set(NOW - timedelta(hours=2))
    a = service.add("Close the Hooli positions", area="Personal admin", due_at=NOW + timedelta(hours=2))
    b = service.add("Ask dad about the intro", area="Personal admin", due_at=NOW + timedelta(hours=3))
    c = service.add("Fill the OSU form", area="School", due_at=NOW - timedelta(hours=1))
    d = service.add("Next week thing", area="School", due_at=NOW + timedelta(days=5))
    clock.set(NOW)
    answers = {f"move #{t.id} to tomorrow": {"action": "reschedule", "task": {"id": t.id, "text": None},
                                             "when": "tomorrow"} for t in (a, b, c, d)}
    link, vapi, _ = make(service, {"commands": [f"move due today except #{b.id} to tomorrow"]}, answers)
    vapi.calls = [a_call("c-bulk", text=f"User: move everything today to tomorrow except the dad one\n"
                                        f"AI: Got it: moving everything due today to tomorrow, except number {b.id}.")]
    res = run(link.pull(NOW))[0]
    assert res.commands == [f"move #{a.id} to tomorrow", f"move #{c.id} to tomorrow"]
    for t in (a, c):
        assert service.get(t.id).due_at.astimezone(ET).date() == (NOW + timedelta(days=1)).date()
    assert service.get(b.id).due_at == b.due_at and service.get(d.id).due_at == d.due_at


def test_bulk_move_needs_the_spoken_confirmation(service, items):
    link, vapi, _ = make(service, {"commands": ["move due today to tomorrow"]})
    vapi.calls = [a_call("c-bulk2", text="User: move everything\nAI: Which ones?")]
    res = run(link.pull(NOW))[0]
    assert res.commands == [] and res.dropped == ["move due today to tomorrow"]


def test_promised_but_nothing_applied_says_so(service, items):
    # 10-31: two "Got it"s, nothing moved, and Discord just said "no changes"
    link, vapi, _ = make(service, {"commands": []})
    vapi.calls = [a_call("c-lost", text="User: move it all\nAI: Got it. Moving all tasks due today to "
                                        "tomorrow, November first. Anything else?")]
    text = voice.render(run(link.pull(NOW))[0])
    assert "NONE of them were applied" in text and "Moving all tasks due today" in text


def test_speakable_codes_and_cut_titles():
    assert voice.speakable("Approve PI-47 grouping") == "Approve P I 47 grouping"
    assert voice.speakable("fix backup_prune") == "fix backup prune"
    assert voice.cut("Continue Keystone (Anthony's notes)", 22) == "Continue Keystone"


def test_payload_hangs_up_by_tool_not_by_word():
    body = voice.assistant_payload("x")
    assert body["endCallPhrases"] == [] and {"type": "endCall"} in body["model"]["tools"]
    assert "Claude" in body["transcriber"]["keyterm"]
    assert body["startSpeakingPlan"]["smartEndpointingPlan"]["provider"] == "livekit"
    assert "alright" not in body["stopSpeakingPlan"]["acknowledgementPhrases"]
    p = voice.build_prompt("x", "4821", NOW)
    assert 'say "Go ahead."' not in p and "Anything else?" in p and '"Goodbye."' not in p
