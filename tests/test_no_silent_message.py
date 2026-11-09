"""The bot never silently ignores a message from Max in #inbox or #today (Phase 1 did, for any
#inbox message without add:/park:/idea:). Exactly one reply each; others stay ignored."""
import asyncio
import sqlite3
from datetime import datetime

import pytest

discord = pytest.importorskip("discord")

import fakes  # noqa: E402
from dispatch import bot as botmod  # noqa: E402
from dispatch.clock import ET  # noqa: E402
from dispatch.config import Settings  # noqa: E402

INBOX, TODAY, SCORE = 1, 2, 3


def make_bot(service, clock, tmp_path, answers=None, raise_for=(), webhook=fakes.WEBHOOK_ID):
    s = Settings(discord_token="x", guild_id=5, max_user_id=fakes.MAX_ID,
                 heartbeat_path=tmp_path / "hb.json", inbox_webhook_id=webhook)
    b = botmod.DispatchBot(s, service, clock, router=fakes.router(service, answers, raise_for))
    b.inbox_channel = fakes.Channel(INBOX, "inbox")
    b.today_channel = fakes.Channel(TODAY, "today")
    return b


MAX = fakes.User(fakes.MAX_ID)
ANSWERS = {"done #3": {"action": "done", "task": {"id": 3}},
           "what's due today": {"action": "query", "kind": "due_today"},
           "move the report draft to tomorrow": {"action": "reschedule",
                                                 "task": {"id": None, "text": "the report draft"},
                                                 "when": "tomorrow"}}
# includes: a bare number with no pending question ("7"), then an ambiguous request followed by
# the number that answers it ("1")
CASES = ["add: email prof by Friday", "park: learn Blender", "idea: fantasy highlight reel",
         "add:", "done #3", "what's due today", "blah blah", "qwen explodes", "7", "thanks",
         "\U0001F44D", "", "move the report draft to tomorrow", "1"]


@pytest.mark.parametrize("channel", [INBOX, TODAY])
def test_every_message_from_max_gets_exactly_one_reply(service, clock, tmp_path, channel):
    clock.set(datetime(2026, 10, 5, 9, 0, tzinfo=ET))
    for i in range(2):
        service.add(f"report draft v{i}", area="School")
    service.add("the other one", area="School")

    async def go():
        b = make_bot(service, clock, tmp_path, ANSWERS, raise_for={"qwen explodes"})
        ch = b.inbox_channel if channel == INBOX else b.today_channel
        out = []
        for text in CASES:
            m = fakes.Message(ch, MAX, text)
            await b.on_message(m)
            out.append((text, m.replies))
        return out

    out = asyncio.run(go())
    for text, replies in out:
        assert len(replies) == 1, f"{text!r} got {len(replies)} replies"
        body, kw = replies[0]
        assert body.strip(), f"{text!r} got an empty reply"
        assert kw["mention_author"] is False
    said = dict((t, r[0][0]) for t, r in out)
    assert said["move the report draft to tomorrow"].startswith("Which one?")
    assert said["1"].startswith("Moved #1 report draft v0")
    assert said["qwen explodes"] == said["blah blah"] == said["7"]   # the hint
    rows = service.conn.execute("SELECT COUNT(*) FROM events WHERE kind='created' AND actor='chat'").fetchone()[0]
    assert rows == 3                                    # add:, park:, idea: in this channel


def test_add_confirmation_has_an_undo_button(service, clock, tmp_path):
    async def go():
        b = make_bot(service, clock, tmp_path)
        m = fakes.Message(b.today_channel, MAX, "add: call Sam tomorrow 3pm")
        await b.on_message(m)
        return m.replies[0]

    body, kw = asyncio.run(go())
    assert body.startswith("Added #1 call Sam") and "Guessed: payoff 4/5" in body
    ids = [c.custom_id for c in kw["view"].children]
    ev = service.conn.execute("SELECT id FROM events WHERE kind='created'").fetchone()[0]
    assert ids == [f"undo:{ev}"]


@pytest.mark.parametrize("who,channel,webhook", [
    (fakes.User(fakes.OTHER_ID), INBOX, None),        # someone else
    (MAX, SCORE, None),                               # another channel
    (fakes.User(fakes.MAX_ID + 1, bot=True), TODAY, None),   # the bot itself / other bots
    (fakes.User(4242, bot=True), INBOX, 12345),        # a different webhook
])
def test_others_are_ignored(service, clock, tmp_path, who, channel, webhook):
    async def go():
        b = make_bot(service, clock, tmp_path)
        ch = {INBOX: b.inbox_channel, TODAY: b.today_channel}.get(channel) or fakes.Channel(SCORE, "scoreboard")
        m = fakes.Message(ch, who, "add: sneaky", webhook_id=webhook)
        await b.on_message(m)
        return m.replies

    assert asyncio.run(go()) == []
    assert service.open_tasks() == []


def test_a_replayed_message_is_handled_once(service, clock, tmp_path):
    async def go():
        b = make_bot(service, clock, tmp_path)
        m = fakes.Message(b.inbox_channel, MAX, "add: once")
        await b.on_message(m)
        await b.on_message(m)                           # gateway + history both deliver it
        return m.replies

    assert len(asyncio.run(go())) == 1 and len(service.open_tasks()) == 1


def test_startup_catch_up_replays_only_new_inbox_messages(service, clock, tmp_path):
    async def go():
        b = make_bot(service, clock, tmp_path)
        old = fakes.Message(b.inbox_channel, MAX, "add: old one", mid=500)
        b.inbox_channel._history = [old]
        assert await b.replay_inbox() == 0              # first run ever: remember, don't replay
        assert service.meta_get(botmod.META_INBOX_SEEN) == "500"
        new1 = fakes.Message(b.inbox_channel, MAX, "add: sent while down", mid=501)
        siri = fakes.Message(b.inbox_channel, fakes.User(77, bot=True), "call mom", mid=502,
                             webhook_id=fakes.WEBHOOK_ID)
        other = fakes.Message(b.inbox_channel, fakes.User(fakes.OTHER_ID), "add: nope", mid=503)
        mine = fakes.Message(b.inbox_channel, fakes.User(88, bot=True), "Added #1 ...", mid=504)
        b.inbox_channel._history = [old, new1, siri, other, mine]
        n = await b.replay_inbox()
        return n, old, new1, siri, other

    n, old, new1, siri, other = asyncio.run(go())
    assert n == 2 and len(new1.replies) == 1 and len(siri.replies) == 1
    assert old.replies == [] and other.replies == []
    assert sorted(t.title for t in service.open_tasks()) == ["call mom", "sent while down"]
    assert service.meta_get(botmod.META_INBOX_SEEN) == "504"   # r3: mark moves past skipped posts



# ---- review r1 should-fixes: mark "answered" only after the reply; page the catch-up ---------
def test_message_is_marked_answered_only_after_the_reply(service, clock, tmp_path):
    async def go():
        b = make_bot(service, clock, tmp_path)
        b.service.meta_set(botmod.META_INBOX_SEEN, "100")
        failing = fakes.Message(b.inbox_channel, MAX, "add: first", mid=200)

        async def broken_reply(*a, **k):
            raise RuntimeError("discord down")
        failing.reply = broken_reply
        await b.on_message(failing)
        after_fail = b.service.meta_get(botmod.META_INBOX_SEEN)
        ok = fakes.Message(b.inbox_channel, MAX, "add: second", mid=150)   # older id, replied
        await b.on_message(ok)
        return after_fail, b.service.meta_get(botmod.META_INBOX_SEEN)

    after_fail, after_ok = asyncio.run(go())
    assert after_fail == "100"            # reply failed -> not marked -> replayed at next start
    assert after_ok == "150"
    assert len(service.open_tasks()) == 2

    async def replay():                   # next start: the catch-up delivers #200 again
        b = make_bot(service, clock, tmp_path)
        m = fakes.Message(b.inbox_channel, MAX, "add: first", mid=200)
        b.inbox_channel._history = [m]
        await b.replay_inbox()
        return m.replies
    replies = asyncio.run(replay())
    assert len(replies) == 1 and replies[0][0].startswith(botmod.AGAIN)
    assert len(service.open_tasks()) == 2  # review r2: the replay re-sends, it does not add again


def test_database_busy_on_marking_still_replies(service, clock, tmp_path, monkeypatch):
    async def go():
        b = make_bot(service, clock, tmp_path)
        b.service.meta_set(botmod.META_INBOX_SEEN, "1")
        real = b.service.meta_set

        def busy(key, value):
            if key == botmod.META_INBOX_SEEN:
                raise sqlite3.OperationalError("database is locked")
            return real(key, value)
        monkeypatch.setattr(b.service, "meta_set", busy)
        m = fakes.Message(b.inbox_channel, MAX, "add: still answered", mid=300)
        await b.on_message(m)
        return m.replies

    replies = asyncio.run(go())
    assert len(replies) == 1 and replies[0][0].startswith("Added")


def test_router_crash_still_gets_the_fallback_reply(service, clock, tmp_path):
    async def go():
        b = make_bot(service, clock, tmp_path)

        async def boom(*a, **k):
            raise RuntimeError("bug")
        b.router.handle = boom
        m = fakes.Message(b.today_channel, MAX, "hello")
        await b.on_message(m)
        return m.replies

    [(text, _)] = asyncio.run(go())
    assert text.startswith("Something went wrong on my side")


def test_catch_up_pages_past_50_and_covers_today_too(service, clock, tmp_path):
    async def go():
        b = make_bot(service, clock, tmp_path)
        b.service.meta_set(botmod.META_INBOX_SEEN, "1000")
        b.service.meta_set(botmod.META_TODAY_SEEN, "5000")
        inbox = [fakes.Message(b.inbox_channel, MAX, f"add: inbox item {i}", mid=1001 + i) for i in range(60)]
        today = [fakes.Message(b.today_channel, MAX, "thanks", mid=5001),
                 fakes.Message(b.today_channel, fakes.User(fakes.OTHER_ID), "add: not max", mid=5002)]
        b.inbox_channel._history = inbox
        b.today_channel._history = today
        n = await b.replay_inbox()
        return n, inbox, today

    n, inbox, today = asyncio.run(go())
    assert n == 61 and all(len(m.replies) == 1 for m in inbox) and len(today[0].replies) == 1
    assert today[1].replies == []
    assert len(service.open_tasks()) == 60
    assert service.meta_get(botmod.META_INBOX_SEEN) == "1060"
    assert service.meta_get(botmod.META_TODAY_SEEN) == "5002"  # r3: past the skipped message


@pytest.mark.parametrize("ai_up", [True, False])
def test_unprefixed_inbox_message_gets_a_one_line_hint_with_the_add_form(service, clock, tmp_path,
                                                                         ai_up):
    """A message in #inbox without add:/park:/idea: (here: the colon forgotten) is never ignored:
    one single-line reply that shows the correct form, and nothing is added."""
    texts = ["add call Sam tomorrow", "call Sam tomorrow"]

    async def go():
        b = make_bot(service, clock, tmp_path, raise_for=() if ai_up else texts)
        out = []
        for text in texts:
            m = fakes.Message(b.inbox_channel, MAX, text)
            await b.on_message(m)
            out.append(m.replies)
        return out

    for replies in asyncio.run(go()):
        [(body, _)] = replies
        assert "\n" not in body.strip() and 'add: ...' in body
    assert service.open_tasks() == []
