"""Review r2 REQUIRED: every message from Max is EXECUTED exactly once and never goes silent,
even if the reply fails or the process dies between the action, the reply and the mark.

Mechanism (dispatch/bot.py on_message): events written while handling a message carry
{"msg": <id>} in the same transaction as the change; the reply is stored as a handled record
right after the router returns; any later delivery re-sends instead of acting again."""
import asyncio
import json
from datetime import datetime, timedelta

import pytest

discord = pytest.importorskip("discord")

import fakes  # noqa: E402
from dispatch import bot as botmod  # noqa: E402
from dispatch import db, rules  # noqa: E402
from dispatch.clock import ET  # noqa: E402
from dispatch.config import Settings  # noqa: E402

NOW = datetime(2026, 10, 5, 10, 0, tzinfo=ET)
MAX = fakes.User(fakes.MAX_ID)


def make_bot(service, clock, tmp_path, answers=None):
    s = Settings(discord_token="x", guild_id=5, max_user_id=fakes.MAX_ID,
                 heartbeat_path=tmp_path / "hb.json", inbox_webhook_id=fakes.WEBHOOK_ID)
    b = botmod.DispatchBot(s, service, clock, router=fakes.router(service, answers or {}))
    b.inbox_channel = fakes.Channel(1, "inbox")
    b.today_channel = fakes.Channel(2, "today")
    return b


def failing(msg):
    async def broken(*a, **k):
        raise RuntimeError("discord 503")
    msg.reply = broken
    return msg


def restart(service, clock, tmp_path, answers=None):
    """A new bot process: fresh in-memory state, same database."""
    return make_bot(service, clock, tmp_path, answers)


def deliver_after_restart(service, clock, tmp_path, text, mid, answers=None, channel="inbox"):
    async def go():
        b = restart(service, clock, tmp_path, answers)
        ch = b.inbox_channel if channel == "inbox" else b.today_channel
        m = fakes.Message(ch, MAX, text, mid=mid)
        ch._history = [m]
        n = await b.replay_inbox()                     # the startup catch-up
        return n, m.replies
    return asyncio.run(go())


def first_delivery_fails(service, clock, tmp_path, text, mid, answers=None, channel="inbox"):
    async def go():
        b = make_bot(service, clock, tmp_path, answers)
        b.service.meta_set(botmod.META_INBOX_SEEN, "100")
        b.service.meta_set(botmod.META_TODAY_SEEN, "100")
        ch = b.inbox_channel if channel == "inbox" else b.today_channel
        await b.on_message(failing(fakes.Message(ch, MAX, text, mid=mid)))
    asyncio.run(go())


@pytest.fixture
def at_now(clock):
    clock.set(NOW)
    return clock


def test_add_with_a_failed_reply_is_not_added_again_on_replay(service, at_now, tmp_path):
    first_delivery_fails(service, at_now, tmp_path, "add: buy milk", 200)
    assert [t.title for t in service.open_tasks()] == ["buy milk"]
    assert service.meta_get(botmod.META_INBOX_SEEN) == "100"          # not marked: will replay
    n, replies = deliver_after_restart(service, at_now, tmp_path, "add: buy milk", 200)
    assert n == 1 and len(replies) == 1                                 # never silent
    text, kw = replies[0]
    assert text.startswith(botmod.AGAIN + "Added #1 buy milk")
    created = db.events_for(service.conn, 1)[0]
    assert [c.custom_id for c in kw["view"].children] == [f"undo:{created['id']}"]
    assert [t.title for t in service.open_tasks()] == ["buy milk"]    # executed exactly once
    assert service.meta_get(botmod.META_INBOX_SEEN) == "200"          # marked after the resend


def test_snooze_with_a_failed_reply_is_not_snoozed_again(service, at_now, tmp_path):
    t = service.add("pay the ticket", area="Personal admin")
    ans = {"snooze task 1 until 3pm": {"action": "snooze", "task": {"id": 1}, "when": "3pm"}}
    first_delivery_fails(service, at_now, tmp_path, "snooze task 1 until 3pm", 300, ans, channel="today")
    assert service.get(t.id).snooze_count == 1
    n, replies = deliver_after_restart(service, at_now, tmp_path, "snooze task 1 until 3pm", 300, ans,
                                       channel="today")
    assert n == 1 and replies[0][0].startswith(botmod.AGAIN + "Snoozed #1")
    assert service.get(t.id).snooze_count == 1                          # ladder not consumed twice


def test_undo_that_with_a_failed_reply_never_undoes_a_second_change(service, at_now, tmp_path):
    a = service.add("first", area="School")
    b = service.add("second", area="School")
    service.mark_done(a.id, actor="button")                           # older change
    service.snooze(b.id, rules.PLUS2H, actor="button")                 # newest change
    ans = {"undo that": {"action": "undo"}}
    first_delivery_fails(service, at_now, tmp_path, "undo that", 400, ans)
    assert service.get(b.id).snooze_count == 0 and service.get(a.id).status == "done"
    n, replies = deliver_after_restart(service, at_now, tmp_path, "undo that", 400, ans)
    assert n == 1 and replies[0][0].startswith(botmod.AGAIN + "Undid the snooze")
    assert service.get(a.id).status == "done"                           # NOT undone by the replay
    undone = service.conn.execute("SELECT COUNT(*) FROM events WHERE kind='undone'").fetchone()[0]
    assert undone == 1


def test_crash_between_the_action_and_the_handled_record(service, at_now, tmp_path):
    """The process dies after the change committed but before the reply was stored: the tagged
    events alone stop a second execution, and a reply is rebuilt from them."""
    first_delivery_fails(service, at_now, tmp_path, "add: renew permit", 500)
    service.meta_set(botmod.HANDLED_PREFIX + "500", None)             # as if never written
    n, replies = deliver_after_restart(service, at_now, tmp_path, "add: renew permit", 500)
    assert n == 1 and replies[0][0] == botmod.AGAIN + "Already done: created #1."
    assert len(service.open_tasks()) == 1
    assert replies[0][1]["view"].children[0].custom_id.startswith("undo:")


def test_crash_between_the_reply_and_the_mark(service, at_now, tmp_path, monkeypatch):
    """Reply delivered, process dies before the high-water mark is written: the catch-up
    delivers the message again - it is re-answered from the record, not re-executed."""
    async def go():
        b = make_bot(service, at_now, tmp_path)
        b.service.meta_set(botmod.META_INBOX_SEEN, "100")
        monkeypatch.setattr(b, "_mark_answered", lambda m: None)       # "crash" before marking
        m = fakes.Message(b.inbox_channel, MAX, "add: call the landlord", mid=600)
        await b.on_message(m)
        return m.replies
    first = asyncio.run(go())
    monkeypatch.undo()
    assert first[0][0].startswith("Added #1") and service.meta_get(botmod.META_INBOX_SEEN) == "100"
    n, replies = deliver_after_restart(service, at_now, tmp_path, "add: call the landlord", 600)
    assert n == 1 and replies[0][0].startswith(botmod.AGAIN + "Added #1")
    assert len(service.open_tasks()) == 1 and service.meta_get(botmod.META_INBOX_SEEN) == "600"


def test_no_change_messages_are_resent_too_and_never_silent(service, at_now, tmp_path):
    first_delivery_fails(service, at_now, tmp_path, "asdf", 700)
    n, replies = deliver_after_restart(service, at_now, tmp_path, "asdf", 700)
    assert n == 1 and replies[0][0].startswith(botmod.AGAIN + "Couldn't parse that")
    chats = service.conn.execute("SELECT COUNT(*) FROM events WHERE kind='chat'").fetchone()[0]
    assert chats == 1                                                   # not handled twice


def test_siri_add_is_executed_once(service, at_now, tmp_path):
    async def go(fail):
        b = make_bot(service, at_now, tmp_path)
        if fail:
            b.service.meta_set(botmod.META_INBOX_SEEN, "100")
        m = fakes.Message(b.inbox_channel, fakes.User(4321, bot=True), "call mom tomorrow 3pm",
                          webhook_id=fakes.WEBHOOK_ID, mid=800)
        if fail:
            failing(m)
            await b.on_message(m)
            return []
        b.inbox_channel._history = [m]
        await b.replay_inbox()
        return m.replies
    asyncio.run(go(True))
    replies = asyncio.run(go(False))
    assert len(replies) == 1 and len(service.open_tasks()) == 1


def test_undo_button_on_a_resent_reply_only_if_it_still_applies(service, at_now, tmp_path):
    first_delivery_fails(service, at_now, tmp_path, "add: water plants", 900)
    created = db.events_for(service.conn, 1)[0]["id"]
    service.undo(created, actor="button")                               # undone meanwhile
    n, replies = deliver_after_restart(service, at_now, tmp_path, "add: water plants", 900)
    assert n == 1 and "view" not in replies[0][1]                       # no stale Undo button


def test_event_tags_are_per_task_not_global(service, at_now):
    """A coroutine running while a message is being handled (the minute loop, a button press)
    must never be tagged with that message's id."""
    async def handler():
        with db.event_context(msg=111):
            await asyncio.sleep(0.01)
            service.log(None, "chat", actor="chat", text="in handler")

    async def other():
        await asyncio.sleep(0.005)
        service.log(None, "chat", actor="scheduler", text="elsewhere")

    async def both():
        await asyncio.gather(handler(), other())
    asyncio.run(both())
    tagged = db.events_for_message(service.conn, 111)
    assert [json.loads(r["detail"])["text"] for r in tagged] == ["in handler"]


def test_handled_records_are_pruned_after_7_days(service, at_now, tmp_path):
    first_delivery_fails(service, at_now, tmp_path, "add: a", 1000)
    at_now.advance(timedelta(days=8))
    first_delivery_fails(service, at_now, tmp_path, "add: b", 1001)
    keys = [r[0] for r in service.conn.execute("SELECT key FROM meta WHERE key LIKE 'handled:%'")]
    assert keys == ["handled:1001"]



def test_today_catch_up_skips_bot_posts_and_moves_the_mark_past_them(service, at_now, tmp_path):
    """review r2 should-fix 5: #today is mostly the bot's own posts - skipped cheaply, and the
    mark moves past them once Max's messages are answered (the next walk starts later)."""
    async def go():
        b = make_bot(service, at_now, tmp_path)
        b.service.meta_set(botmod.META_TODAY_SEEN, "100")
        me = fakes.User(9, bot=True)
        posts = [fakes.Message(b.today_channel, me, f"nag {i}", mid=101 + i) for i in range(30)]
        mine = fakes.Message(b.today_channel, MAX, "thanks", mid=150)
        b.today_channel._history = posts + [mine]
        n = await b.replay_inbox()
        return n, posts, mine
    n, posts, mine = asyncio.run(go())
    assert n == 1 and len(mine.replies) == 1 and all(p.replies == [] for p in posts)
    assert service.meta_get(botmod.META_TODAY_SEEN) == "150"


def test_catch_up_keeps_the_mark_before_a_message_whose_reply_failed(service, at_now, tmp_path):
    async def go():
        b = make_bot(service, at_now, tmp_path)
        b.service.meta_set(botmod.META_TODAY_SEEN, "100")
        bad = failing(fakes.Message(b.today_channel, MAX, "add: x", mid=120))
        later_bot_post = fakes.Message(b.today_channel, fakes.User(9, bot=True), "nag", mid=130)
        b.today_channel._history = [bad, later_bot_post]
        await b.replay_inbox()
    asyncio.run(go())
    assert service.meta_get(botmod.META_TODAY_SEEN) == "100"           # #120 will be retried
    n, replies = deliver_after_restart(service, at_now, tmp_path, "add: x", 120, channel="today")
    assert n == 1 and replies[0][0].startswith(botmod.AGAIN) and len(service.open_tasks()) == 1


# ---- Phase 2.1: the new mutations obey the same exactly-once rule --------------------------
def test_set_mode_with_a_failed_reply_is_set_once(service, at_now, tmp_path):
    for i in range(4):
        service.add(f"errand {i}", area="Personal admin", place="out")
    service.add("home thing", area="Personal admin", place="home", estimate_minutes=20)
    ans = {"sick day today": {"action": "set_mode", "mode": "sick"}}
    first_delivery_fails(service, at_now, tmp_path, "sick day today", 1100, ans)
    n, replies = deliver_after_restart(service, at_now, tmp_path, "sick day today", 1100, ans)
    assert n == 1 and replies[0][0].startswith(botmod.AGAIN + "Sick/home day:")
    assert replies[0][1]["view"].children[0].custom_id.startswith("undo:")
    modes = service.conn.execute("SELECT COUNT(*) FROM events WHERE kind='mode_set'").fetchone()[0]
    assert modes == 1


def test_did_with_a_failed_reply_logs_one_row(service, at_now, tmp_path):
    first_delivery_fails(service, at_now, tmp_path, "did: called the bank", 1200)
    n, replies = deliver_after_restart(service, at_now, tmp_path, "did: called the bank", 1200)
    assert n == 1 and replies[0][0].startswith(botmod.AGAIN + "Logged as done")
    assert service.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


def test_resent_minutes_reply_keeps_its_start_and_skip_buttons(service, at_now, tmp_path):
    t = service.add("quick one", area="School", estimate_minutes=10)
    ans = {"I've got 20 minutes": {"action": "minutes", "minutes": 20}}
    first_delivery_fails(service, at_now, tmp_path, "I've got 20 minutes", 1300, ans)
    n, replies = deliver_after_restart(service, at_now, tmp_path, "I've got 20 minutes", 1300, ans)
    assert [c.custom_id for c in replies[0][1]["view"].children] == [f"start:{t.id}",
                                                                     f"skip:{t.id}"]


def test_button_driven_swap_writes_untagged_events(service, at_now, tmp_path):
    a = service.add("long one", area="School", estimate_minutes=90)
    service.add("b", area="School")
    service.add("c", area="School")
    service.add("short queued", area="School", estimate_minutes=10)
    b = make_bot(service, at_now, tmp_path)
    it = fakes.Interaction()
    asyncio.run(b.handle_swap_reason(it, "long", a.id))
    ev = service.conn.execute("SELECT * FROM events WHERE kind='swapped'").fetchone()
    assert ev["actor"] == "button" and "msg" not in json.loads(ev["detail"])
