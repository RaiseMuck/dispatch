"""Plain-English chat with a MOCKED Qwen (a dict: message -> JSON). No network.

Seed ids (import order, deterministic): #1 Strimer return (due Sat 10/3 11:00), #2 Keystone
sessions (10/17), #3 Call Sam, #4 insurance broker, #5 target-margin (parked), #6 monthly
investing, #7 bike fund, #8 lease renewal, #9 THESIS_TRACKER, #10 XYZ, #11 PC workbench
backup, #12 MacBook backup, #13 Mac Cowork duplicates, #14 git loop, #15 1099 tuition lookup,
#16 1099 direct-pay, #17 assets/debts, #18 electroculture. Clock: Mon 10/5 09:00 ET.
"""
import asyncio
import json
from datetime import datetime, timedelta

import pytest

from dispatch import chat, db, qwen, rules
from dispatch.__main__ import SEED_PATH, import_seed
from dispatch.clock import ET

NOW = datetime(2026, 10, 5, 9, 0, tzinfo=ET)
GUESS = qwen.Guess(area="Personal admin", payoff=4, likelihood=0.8, estimate_minutes=20,
                   guessed=["area", "payoff", "likelihood", "estimate_minutes"], model="fake", ms=3)


async def _inline(fn, *a):
    return fn(*a)


def make_router(service, answers: dict):
    calls = []

    def ask(prompt):
        msg = prompt.rsplit('Message: "', 1)[1].rsplit('"', 1)[0]
        calls.append(msg)
        v = answers.get(msg, {"action": "unclear"})
        if isinstance(v, Exception):
            raise v
        return v

    r = chat.Router(service, ask=ask, guess=lambda t, n: GUESS, run=_inline)
    r.calls = calls
    return r


def say(router, text, reply_ids=None):
    return asyncio.run(router.handle(text, reply_ids))


@pytest.fixture
def seeded(service, clock):
    clock.set(NOW - timedelta(days=1))
    import_seed(service, SEED_PATH)
    clock.set(NOW)
    return service


def last_event(service):
    return service.conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT 1").fetchone()


def chat_events(service):
    return service.conn.execute("SELECT * FROM events WHERE kind='chat' ORDER BY id").fetchall()


T = lambda i=None, text=None: {"id": i, "text": text}   # noqa: E731


def _check_done(s, r):
    assert s.get(3).status == "done" and r.text.startswith("Done: #3")


def _check_kill(s, r):
    assert s.get(18).status == "killed"


def _check_snooze(s, r):
    t = s.get(11)
    assert t.next_nag_at == NOW.replace(hour=15) and t.snooze_count == 1 and t.due_at is None


def _check_reschedule(s, r):
    t = s.get(1)
    assert t.due_at == datetime(2026, 10, 10, 13, 0, tzinfo=ET) and t.status == "queued"
    assert "Up next" in r.text                          # freed its active slot


def _check_estimate(s, r):
    assert s.get(12).estimate_minutes == 20


def _check_priority(s, r):
    assert s.get(9).payoff == 2


def _check_area(s, r):
    assert s.get(6).area == "Personal admin"


def _check_do_now(s, r):
    assert s.get(2).next_nag_at == NOW + timedelta(hours=1)


def _check_shift(s, r):
    assert r.text.startswith("Quiet until 4:00 PM")
    assert all(t.next_nag_at >= NOW.replace(hour=16) for t in s.by_status("active"))


def _check_add(s, r):
    t = s.get(r.task_ids[0])
    assert t.title == "renew the parking permit" and t.due_at == datetime(2026, 10, 9, 9, 0, tzinfo=ET)
    assert t.guessed == ["area", "payoff", "likelihood", "estimate_minutes"] and t.payoff == 4
    assert "Guessed: payoff 4/5, 80% likely to matter, ~20 min" in r.text


ACTION_CASES = [
    ("done #3", {"action": "done", "task": T(3)}, "done", _check_done),
    ("kill the electroculture thing", {"action": "kill", "task": T(None, "the electroculture thing")},
     "killed", _check_kill),
    ("snooze #11 until 3pm", {"action": "snooze", "task": T(11), "when": "until 3pm"}, "snoozed",
     _check_snooze),
    ("move the Strimer return to Saturday 1pm", {"action": "reschedule",
     "task": T(None, "the Strimer return"), "when": "Saturday 1pm"}, "rescheduled", _check_reschedule),
    ("the MacBook backup is a 20 minute job", {"action": "re_estimate",
     "task": T(None, "the MacBook backup"), "minutes": 20}, "estimated", _check_estimate),
    ("low priority: THESIS_TRACKER renumbering", {"action": "set_priority",
     "task": T(None, "THESIS_TRACKER renumbering"), "payoff": 2}, "prioritized", _check_priority),
    ("#6 is actually Personal admin", {"action": "set_area", "task": T(6), "area": "Personal admin"},
     "area_set", _check_area),
    ("starting the Keystone sessions now", {"action": "do_now", "task": T(None, "the Keystone sessions")},
     "do_now", _check_do_now),
    ("I'm slammed until 4, move everything after that", {"action": "shift_after", "when": "4"},
     "shifted", _check_shift),
    ("add renew the parking permit by Friday", {"action": "add", "title": "renew the parking permit",
     "when": "by Friday"}, "created", _check_add),
]


@pytest.mark.parametrize("msg,answer,kind,check", ACTION_CASES, ids=[c[2] for c in ACTION_CASES])
def test_each_changing_action_executes_logs_and_offers_undo(seeded, msg, answer, kind, check):
    r = say(make_router(seeded, {msg: answer}), msg)
    ev = seeded.conn.execute("SELECT * FROM events WHERE id=?", (r.undo_event_id,)).fetchone()
    assert ev["kind"] == kind and ev["actor"] == "chat"
    assert r.text and r.action == answer["action"]
    check(seeded, r)
    before_undo = r.undo_event_id
    seeded.undo(before_undo)                         # and the Undo button would reverse it
    assert seeded.conn.execute("SELECT undone_by FROM events WHERE id=?",
                               (before_undo,)).fetchone()[0] is not None


@pytest.mark.parametrize("msg,answer,expect", [
    ("what's due today", {"action": "query", "kind": "due_today"}, "#1 Return the Strimer"),
    ("what's on my plate this week", {"action": "query", "kind": "due_week"},
     "Due in the next 7 days (1):\n#1 Return the Strimer"),
    ("list", {"action": "query", "kind": "active"}, "Your active three (3)"),
    ("what's queued", {"action": "query", "kind": "queued"}, "Queued (waiting for a slot) (14)"),
    ("what's parked", {"action": "query", "kind": "parked"}, "#5 Pick the real target-margin"),
    ("thanks", {"action": "unclear"}, "Couldn't parse that"),
])
def test_queries_and_unclear_change_nothing_but_reply_and_log(seeded, msg, answer, expect):
    before = [tuple(x) for x in seeded.conn.execute("SELECT * FROM tasks")]
    r = say(make_router(seeded, {msg: answer}), msg)
    assert expect in r.text and r.undo_event_id is None
    assert [tuple(x) for x in seeded.conn.execute("SELECT * FROM tasks")] == before
    ev = chat_events(seeded)[-1]
    d = json.loads(ev["detail"])
    assert d["text"] == msg and d["action"] in (f"query:{answer.get('kind')}", "unclear")


def test_undo_by_text(seeded):
    r = make_router(seeded, {"done #3": {"action": "done", "task": T(3)},
                             "undo that": {"action": "undo"}})
    say(r, "done #3")
    assert seeded.get(3).status == "done"
    out = say(r, "undo that")
    assert out.action == "undo" and seeded.get(3).status == "active" and "Undid the done" in out.text
    assert "Nothing to undo" in say(r, "undo that").text


# ---- which task (wrong-task = 0) ------------------------------------------------------------
def test_literal_id_beats_a_wrong_qwen_id(seeded):
    r = say(make_router(seeded, {"done #7": {"action": "done", "task": T(3)}}), "done #7")
    assert seeded.get(7).status == "done" and seeded.get(3).status == "active"


def test_qwen_id_not_in_the_message_is_only_a_hint(seeded):
    # Qwen claims #4 but the words say "the Strimer return" -> #1, never #4
    r = say(make_router(seeded, {"finished the Strimer return": {"action": "done",
                                 "task": T(4, "the Strimer return")}}), "finished the Strimer return")
    assert seeded.get(1).status == "done" and seeded.get(4).status != "done"


def test_ambiguous_asks_then_a_number_executes_on_that_one(seeded, clock):
    ans = {"do the 1099 thing tomorrow": {"action": "reschedule", "task": T(None, "the 1099 thing"),
                                         "when": "tomorrow"}}
    r = make_router(seeded, ans)
    out = say(r, "do the 1099 thing tomorrow")
    assert out.action == "ambiguous" and "1) #15" in out.text and "2) #16" in out.text
    assert seeded.get(15).due_at is None and seeded.get(16).due_at is None   # nothing executed
    clock.advance(timedelta(minutes=3))
    out = say(r, "2")
    assert out.action == "reschedule" and seeded.get(16).due_at == datetime(2026, 10, 6, 9, 0, tzinfo=ET)
    assert seeded.get(15).due_at is None
    assert r.calls == ["do the 1099 thing tomorrow"]    # "2" never went to the AI


def test_pending_choice_expires_after_10_minutes(seeded, clock):
    ans = {"done with the backup": {"action": "done", "task": T(None, "the backup")}}
    r = make_router(seeded, ans)
    assert say(r, "done with the backup").action == "ambiguous"
    clock.advance(timedelta(minutes=11))
    out = say(r, "1")                                    # parsed fresh -> unclear, nothing done
    assert out.action == "unclear" and seeded.get(11).status == "queued"
    assert r.calls[-1] == "1"


def test_other_message_clears_pending(seeded):
    r = make_router(seeded, {"done with the backup": {"action": "done", "task": T(None, "the backup")}})
    say(r, "done with the backup")
    say(r, "thanks")
    assert say(r, "1").action == "unclear"


def test_portfolio_one_is_ambiguous_and_lists_four(seeded):
    msg = "mark the Portfolio one done"
    out = say(make_router(seeded, {msg: {"action": "done", "task": T(None, "the Portfolio one")}}), msg)
    assert out.action == "ambiguous" and out.text.count(") #") == 4
    assert not seeded.by_status("done")


def test_no_reference_uses_recent_context_else_asks(seeded, clock):
    msg = "that's more like 3 hours"
    ans = {msg: {"action": "re_estimate", "task": None, "minutes": 180},
           "add: call the dentist": {"action": "unclear"}}
    r = make_router(seeded, ans)
    assert say(r, msg).text == chat.ASK_WHICH                  # nothing to refer to yet
    added = say(r, "add: call the dentist")
    tid = added.task_ids[0]
    out = say(r, msg)
    assert out.action == "re_estimate" and seeded.get(tid).estimate_minutes == 180
    clock.advance(timedelta(minutes=31))
    assert say(r, msg).text == chat.ASK_WHICH                  # context expired


def test_discord_reply_to_a_bot_message_names_the_task(seeded):
    msg = "done"
    r = make_router(seeded, {msg: {"action": "done", "task": None}})
    out = say(r, msg, reply_ids=[2])
    assert out.action == "done" and seeded.get(2).status == "done"
    out = say(r, msg, reply_ids=[1, 3])                        # a morning post with several items
    assert out.action == "ambiguous"


def test_not_found(seeded):
    msg = "kill the dentist thing"
    out = say(make_router(seeded, {msg: {"action": "kill", "task": T(None, "the dentist thing")}}), msg)
    assert out.action == "not_found" and "dentist" in out.text
    out = say(make_router(seeded, {"done #99": {"action": "done", "task": T(99)}}), "done #99")
    assert out.action == "not_found"


def test_numbers_that_are_durations_or_times_are_not_ids(seeded):
    msg = "#12 is a 3 hour job"
    out = say(make_router(seeded, {msg: {"action": "re_estimate", "task": T(12), "minutes": 180}}), msg)
    assert seeded.get(12).estimate_minutes == 180 and seeded.get(3).estimate_minutes is None
    assert chat.explicit_ids("snooze task 11 until 3pm, 20 min", {3})[0] == [11]
    assert chat.explicit_ids("snooze 11 until 3pm", set())[0] == []      # bare number: not an id


# ---- dates: the deterministic parser, never the AI -----------------------------------------
def test_next_wednesday_means_the_following_week(seeded):
    # Max's rule: at Mon 10/5 "next Wednesday" = Wed 10/14 (the following calendar week);
    # "this Wednesday" / "Wednesday" = Wed 10/7.
    msg = "push the Keystone sessions to next Wednesday"
    say(make_router(seeded, {msg: {"action": "reschedule", "task": T(None, "the Keystone sessions"),
                                   "when": "next Wednesday"}}), msg)
    assert seeded.get(2).due_at == datetime(2026, 10, 14, 9, 0, tzinfo=ET)
    msg = "move the lease thing to this Wednesday"
    say(make_router(seeded, {msg: {"action": "reschedule", "task": T(None, "the lease thing"),
                                   "when": "this Wednesday"}}), msg)
    assert seeded.get(8).due_at == datetime(2026, 10, 7, 9, 0, tzinfo=ET)


def test_unparseable_time_asks_and_changes_nothing(seeded):
    msg = "move the Strimer return to whenever"
    out = say(make_router(seeded, {msg: {"action": "reschedule", "task": T(None, "the Strimer return"),
                                         "when": "whenever"}}), msg)
    assert out.text == chat.ASK_WHEN and seeded.get(1).due_at.day == 3


def test_ai_time_words_missing_falls_back_to_the_message(seeded):
    msg = "bike money: decide Friday, 2 hrs of work"
    say(make_router(seeded, {msg: {"action": "reschedule", "task": T(None, "bike money"), "when": None}}), msg)
    assert seeded.get(7).due_at == datetime(2026, 10, 9, 9, 0, tzinfo=ET)


def test_snooze_to_a_later_day_is_a_reschedule(seeded):
    msg = "snooze the lease thing until tomorrow"
    out = say(make_router(seeded, {msg: {"action": "snooze", "task": T(None, "the lease thing"),
                                         "when": "tomorrow"}}), msg)
    t = seeded.get(8)
    assert out.action == "snooze" and t.due_at == datetime(2026, 10, 6, 9, 0, tzinfo=ET)
    assert last_event(seeded)["kind"] in ("rescheduled", "promoted")


def test_snooze_ladder_exhausted_gives_the_hint_and_changes_nothing(seeded):
    seeded.snooze(8, rules.PLUS2H)
    seeded.snooze(8, rules.TONIGHT)
    msg = "later tonight for the lease thing"
    before = seeded.get(8)
    out = say(make_router(seeded, {msg: {"action": "snooze", "task": T(None, "the lease thing"),
                                         "when": "later tonight"}}), msg)
    assert out.action == "ladder" and "used both snoozes" in out.text and seeded.get(8) == before


def test_shift_after_moves_only_items_nagging_before_the_time(seeded, clock):
    seeded.do_now(3)                                         # #3 next nag 10:00 -> moves
    seeded.snooze(2, rules.PLUS2H, explicit=NOW.replace(hour=17))    # 17:00 -> stays
    parked_before = seeded.get(5)
    msg = "nothing until 2pm please"
    out = say(make_router(seeded, {msg: {"action": "shift_after", "when": "2pm"}}), msg)
    assert out.text == "Quiet until 2:00 PM - 2 items moved (#1, #3)."
    assert seeded.get(2).next_nag_at == NOW.replace(hour=17)
    assert seeded.get(5) == parked_before
    ev = seeded.conn.execute("SELECT * FROM events WHERE kind='shifted'").fetchone()
    d = json.loads(ev["detail"])
    assert ev["task_id"] is None and [x["id"] for x in d["tasks"]] == [1, 3]
    assert seeded.get(1).due_at == datetime(2026, 10, 3, 11, 0, tzinfo=ET)   # due dates untouched


def test_loose_hour_and_clean_when():
    assert chat.parse_time_phrase("until 4", "", NOW) == NOW.replace(hour=16)
    assert chat.parse_time_phrase("4", "", NOW.replace(hour=3)) == NOW.replace(hour=4)
    assert chat.parse_time_phrase("later tonight", "", NOW) == NOW.replace(hour=20)
    assert chat.parse_minutes("that's more like 3 hours") == 180
    assert chat.parse_minutes("half an hour tops") == 30
    assert chat.parse_minutes("no numbers") is None


# ---- the closed schema -------------------------------------------------------------------
@pytest.mark.parametrize("obj", [
    {"action": "launch_rocket"},
    {"action": "done", "task": {"id": 3}, "mood": "happy"},          # extra key
    {"action": "done", "task": {"id": "3"}},                          # wrong type
    {"action": "done", "task": "#3", "when": 5},
    {"action": "query", "kind": "everything"},
    ["done", 3], None, "done",
])
def test_off_schema_is_unclear(obj):
    assert chat.validate(obj)["action"] == "unclear"


def test_valid_schema_normalises():
    v = chat.validate({"action": "re_estimate", "task": {"id": None, "text": " the backup "},
                       "minutes": 20.0})
    assert v["task"] == {"id": None, "text": "the backup"} and v["minutes"] == 20


def test_ai_down_or_garbage_gives_the_hint_and_executes_nothing(seeded):
    before = [tuple(x) for x in seeded.conn.execute("SELECT * FROM tasks")]
    r = make_router(seeded, {"done #3": RuntimeError("ollama down"),
                             "kill #4": {"action": "kill", "task": {"id": 4}, "extra": 1}})
    assert say(r, "done #3").text == chat.HINT
    assert say(r, "kill #4").text == chat.HINT
    r2 = chat.Router(seeded, ask=lambda p: None, guess=lambda t, n: GUESS, run=_inline)
    assert say(r2, "done #3").text == chat.HINT
    assert [tuple(x) for x in seeded.conn.execute("SELECT * FROM tasks")] == before
    assert len(chat_events(seeded)) == 3


def test_musing_is_never_added(seeded):
    msg = "I should probably call Sam at some point"
    n = len(seeded.open_tasks())
    out = say(make_router(seeded, {msg: {"action": "add", "title": "call Sam"}}), msg)
    assert out.action == "add_unconfirmed" and len(seeded.open_tasks()) == n
    assert seeded.get(3).status == "active"


def test_prefix_add_needs_no_ai_and_shows_guesses(seeded):
    r = make_router(seeded, {})
    out = say(r, "add: call Sam tomorrow 3pm")
    t = seeded.get(out.task_ids[0])
    assert r.calls == [] and t.due_at == datetime(2026, 10, 6, 15, 0, tzinfo=ET)
    assert "Guessed:" in out.text and out.undo_event_id
    r2 = chat.Router(seeded, ask=lambda p: None, guess=lambda t, n: qwen.Guess(), run=_inline)
    out = say(r2, "idea: restaurant analytics")
    assert "Parked" in out.text and "the local AI was off" in out.text
    assert seeded.get(out.task_ids[0]).area == "unsorted"


# ---- regressions from the first live eval (qwen3.5:4b copied a whole title into task.text) ----
@pytest.mark.parametrize("msg,answer", [
    ("do the 1099 thing tomorrow", {"action": "reschedule", "when": "tomorrow",
     "task": {"id": 16, "text": "Decide: pay the PC parts straight from the 1099 income, or pay yourself back from it"}}),
    ("mark the Portfolio one done", {"action": "done",
     "task": {"id": 1, "text": "Return the Strimer Wireless 24-pin at Micro Center (bring receipt"}}),
])
def test_ai_copying_a_title_from_the_list_never_picks_the_task(seeded, msg, answer):
    before = [tuple(x) for x in seeded.conn.execute("SELECT * FROM tasks")]
    out = say(make_router(seeded, {msg: answer}), msg)
    assert out.action == "ambiguous"
    assert [tuple(x) for x in seeded.conn.execute("SELECT * FROM tasks")] == before


def test_bare_number_that_belongs_to_other_words_is_not_an_id(seeded):
    # "3" is part of the title of #17 ("... outside the 3 streaming subscriptions ..."), not task #3
    msg = "done with the 3 streaming subscriptions one"
    out = say(make_router(seeded, {msg: {"action": "done", "task": {"id": 3, "text": "3 streaming subscriptions"}}}), msg)
    assert out.action == "done" and seeded.get(17).status == "done"
    assert seeded.get(3).status == "active"


def test_took_2_tries_never_acts_on_task_2(seeded):
    # review r1 should-fix: a bare number is an id only with '#' or "task/item/number N"
    msg = "done with the 1099 thing, took 2 tries"
    out = say(make_router(seeded, {msg: {"action": "done", "task": {"id": 2, "text": "the 1099 thing"}}}), msg)
    assert out.action == "ambiguous" and seeded.get(2).status == "active"
    assert not seeded.by_status("done")
    msg = "done task 14"
    out = say(make_router(seeded, {msg: {"action": "done", "task": {"id": 14, "text": None}}}), msg)
    assert out.action == "done" and seeded.get(14).status == "done"
    msg = "drop 13"                                   # bare, no words: ask, never guess
    out = say(make_router(seeded, {msg: {"action": "kill", "task": {"id": 13, "text": None}}}), msg)
    assert out.text == chat.ASK_WHICH and seeded.get(13).status == "queued"


def test_model_id_never_breaks_a_near_tie(seeded):
    msg = "done with the backup"                      # #11 and #12 tie on "backup"
    out = say(make_router(seeded, {msg: {"action": "done", "task": {"id": 12, "text": "the backup"}}}), msg)
    assert out.action == "ambiguous" and not seeded.by_status("done")


def test_hash_answer_to_which_one_is_a_task_id_not_an_option(seeded, clock):
    # review r1 required #2: candidates listed as 1) #4 2) #1; answering "#1" must act on #1
    msg = "done with the return call"
    r = make_router(seeded, {})
    seeded.meta_set(chat.PENDING_KEY, json.dumps({"payload": chat.validate(
        {"action": "done", "task": {"id": None, "text": "return call"}}), "text": msg,
        "candidates": [4, 1], "at": clock.now().isoformat()}))    # the reviewer's exact order
    out = say(r, "#1")
    assert out.action == "done" and seeded.get(1).status == "done"
    assert seeded.get(4).status != "done"                         # (#4 was promoted, not closed)


def test_bare_number_that_is_both_an_option_and_a_candidate_id_asks_again(seeded, clock):
    r = make_router(seeded, {})
    pending = {"payload": chat.validate({"action": "done", "task": None}), "text": "x",
               "candidates": [2, 1], "at": clock.now().isoformat()}
    seeded.meta_set(chat.PENDING_KEY, json.dumps(pending))
    out = say(r, "1")                                  # option 1 = #2, but #1 is also a choice
    assert out.action == "clarify" and "#2" in out.text and not seeded.by_status("done")
    out = say(r, "#2")                                 # still pending: answer with the #number
    assert out.action == "done" and seeded.get(2).status == "done" and seeded.get(1).status == "active"
    seeded.meta_set(chat.PENDING_KEY, json.dumps({**pending, "candidates": [15, 16]}))
    out = say(r, "2")                                  # unambiguous index
    assert out.action == "done" and seeded.get(16).status == "done"
    seeded.meta_set(chat.PENDING_KEY, json.dumps({**pending, "candidates": [8, 9]}))
    out = say(r, "#5")                                 # not a choice: asks, keeps the question
    assert out.action == "clarify" and seeded.meta_get(chat.PENDING_KEY)


def test_priority_words_beat_the_ai_number(seeded):
    msg = "low priority: THESIS_TRACKER renumbering"
    say(make_router(seeded, {msg: {"action": "set_priority", "task": {"id": 9, "text": "x"}, "payoff": 4}}), msg)
    assert seeded.get(9).payoff == 2


def test_date_words_inside_the_models_add_title_become_the_due_date(seeded):
    msg = "add renew the parking permit by Friday"
    out = say(make_router(seeded, {msg: {"action": "add", "title": "renew the parking permit by Friday",
                                         "when": None}}), msg)
    t = seeded.get(out.task_ids[0])
    assert t.title == "renew the parking permit" and t.due_at == datetime(2026, 10, 9, 9, 0, tzinfo=ET)



def test_compound_words_match_lookup_and_look_up(seeded):
    # review r2: the held-out miss "push the tuition lookup to Thursday 2pm" (the reported
    # held-out numbers are NOT changed by this; this is a regression test for the fix)
    msg = "push the tuition lookup to Thursday 2pm"
    out = say(make_router(seeded, {msg: {"action": "reschedule", "task": T(None, "the tuition lookup"),
                                         "when": "Thursday 2pm"}}), msg)
    assert out.action == "reschedule" and seeded.get(15).due_at == datetime(2026, 10, 8, 14, 0, tzinfo=ET)
    t15 = seeded.get(15)
    assert chat.match_score("tuition look up", t15) == 1.0
    fake = type(t15)(**{**t15.__dict__, "title": "Fix the log-in page"})
    assert chat.match_score("login page", fake) == 1.0
