"""Finishing a task posts the fresh list in #today: the three as they stand now (with what moved
up) plus anything else due - once per Done (or burst of Dones), whether it came from a button,
chat, a phone call or the CLI. Never for 'did:', an undone Done, a Kill, or in quiet hours."""
import asyncio
from datetime import datetime, timedelta

import pytest

discord = pytest.importorskip("discord")

import fakes  # noqa: E402
from dispatch import bot, scheduler  # noqa: E402
from dispatch.clock import ET  # noqa: E402
from dispatch.config import Settings  # noqa: E402
from dispatch.scheduler import list_after_done  # noqa: E402

NOW = datetime(2026, 10, 5, 9, 0, tzinfo=ET)
MAX = fakes.User(fakes.MAX_ID)


@pytest.fixture
def day(service, clock):
    """Five items: the first three are the active three, the other two wait in the queue."""
    clock.set(NOW)
    ids = [service.add(f"item {i}", area="Personal admin", estimate_minutes=30).id
           for i in range(1, 6)]
    list_after_done(service, NOW)              # first run ever: only sets the mark
    return ids


def make_bot(service, clock, tmp_path, answers=None):
    s = Settings(discord_token="x", guild_id=5, max_user_id=fakes.MAX_ID,
                 heartbeat_path=tmp_path / "hb.json", inbox_webhook_id=fakes.WEBHOOK_ID)
    b = bot.DispatchBot(s, service, clock, router=fakes.router(service, answers or {}))
    b.inbox_channel = fakes.Channel(1, "inbox")
    b.today_channel = fakes.Channel(2, "today")
    b.list_delay = 0
    return b


async def settle(b):
    if b._list_task is not None:
        await b._list_task


def test_first_run_ever_only_sets_the_mark(service, clock):
    clock.set(NOW)
    t = service.add("old", area="Personal admin")
    service.mark_done(t.id)
    assert list_after_done(service, NOW) is None          # history is never posted
    assert service.meta_get(scheduler.META_LIST_DONE).isdigit()


def test_done_gives_the_new_three_with_what_moved_up(service, day):
    active = [t.id for t in service.by_status("active")]
    assert len(active) == 3
    done, up = service.mark_done(active[0])
    msg = list_after_done(service, NOW)
    assert msg.kind == "list" and msg.mention is False
    assert msg.header.startswith(f"Done: #{done.id} {done.title}. Your list now:")
    assert sorted(t.id for t in msg.items) == sorted(active[1:] + [up[0].id])
    assert f"Moved up: #{up[0].id} {up[0].title}." in msg.footer
    assert set(msg.swap_ids()) == {t.id for t in msg.items}   # Swap row like the morning post
    assert list_after_done(service, NOW) is None              # each Done is covered once


def test_several_dones_in_a_row_give_one_list(service, day):
    a, b_ = [t.id for t in service.by_status("active")][:2]
    service.mark_done(a)
    service.mark_done(b_)
    msg = list_after_done(service, NOW)
    assert f"#{a} " in msg.header and f"#{b_} " in msg.header
    assert len(msg.items) == 3
    assert list_after_done(service, NOW) is None


def test_no_list_for_did_kill_or_an_undone_done(service, day):
    service.log_done("called the bank")
    service.kill(service.by_status("active")[0].id)
    t, _ = service.mark_done(service.by_status("active")[0].id)
    service.undo(service.last_event_id)
    assert service.get(t.id).status == "active"
    assert list_after_done(service, NOW) is None


def test_quiet_hours_skip_the_post_and_do_not_save_it_for_later(service, clock, day):
    late = NOW.replace(hour=1) + timedelta(days=1)
    clock.set(late)
    service.mark_done(service.by_status("active")[0].id)
    assert list_after_done(service, late) is None
    clock.set(late.replace(hour=8, minute=5))
    assert list_after_done(service, clock.now()) is None   # the 8 AM post covers it


def test_urgent_items_outside_the_three_are_listed_too(service, clock, day):
    due = service.add("pay rent", area="Home & life", due_at=NOW - timedelta(hours=1),
                      status="queued")
    service.set_fields(due.id, estimate_minutes=600)        # ranks low: stays out of the three
    service.mark_done(service.by_status("active")[0].id)
    active, extra = service.today_list(NOW)
    msg = list_after_done(service, NOW)
    assert [t.id for t in msg.items] == [t.id for t in active + extra]
    if due.id in {t.id for t in extra}:
        assert f"Also due now (outside your three): #{due.id}" in msg.footer


def test_nothing_left(service, clock):
    clock.set(NOW)
    t = service.add("only one", area="Personal admin")
    list_after_done(service, NOW)
    service.mark_done(t.id)
    msg = list_after_done(service, NOW)
    assert msg.items == [] and msg.header.endswith("Nothing else in your three right now.")


def test_done_button_answers_first_then_posts_the_list(service, clock, tmp_path, day):
    first = service.by_status("active")[0]

    async def go():
        b = make_bot(service, clock, tmp_path)
        it = fakes.Interaction()
        await b.handle_button(it, "done", first.id)
        await settle(b)
        return b, it

    b, it = asyncio.run(go())
    assert it.texts()[0].startswith(f"Done: #{first.id}")
    [(text, kw)] = b.today_channel.sent
    assert text.startswith(f"Done: #{first.id} {first.title}. Your list now:")
    assert kw["allowed_mentions"].users is False             # no second buzz
    ids = [c.custom_id for c in kw["view"].children]
    for t in service.by_status("active"):
        assert f"done:{t.id}" in ids and f"swap:{t.id}" in ids


def test_kill_button_posts_no_list(service, clock, tmp_path, day):
    async def go():
        b = make_bot(service, clock, tmp_path)
        await b.handle_button(fakes.Interaction(), "kill", service.by_status("active")[0].id)
        await settle(b)
        return b

    assert asyncio.run(go()).today_channel.sent == []


def test_done_by_chat_replies_then_posts_the_list(service, clock, tmp_path, day):
    tid = service.by_status("active")[1].id
    text = f"done #{tid}"

    async def go():
        b = make_bot(service, clock, tmp_path,
                     {text: {"action": "done", "task": {"id": tid, "text": None}}})
        m = fakes.Message(b.inbox_channel, MAX, text)
        await b.on_message(m)
        await settle(b)
        return b, m

    b, m = asyncio.run(go())
    [(reply, _)] = m.replies
    assert reply.startswith(f"Done: #{tid}")
    [(text_, _)] = b.today_channel.sent
    assert text_.startswith(f"Done: #{tid} ")


def test_done_from_outside_the_bot_is_picked_up_by_the_minute_nudge(service, clock, tmp_path,
                                                                    day):
    """The CLI (another process) closes an item; the bot's next nudge posts the list."""
    tid = service.by_status("active")[2].id

    async def go():
        b = make_bot(service, clock, tmp_path)
        service.mark_done(tid, actor="cli")
        b.nudge_list()
        b.nudge_list()                                      # a pending one is not doubled
        await settle(b)
        b.nudge_list()                                      # nothing new: no second post
        await settle(b)
        return b

    b = asyncio.run(go())
    assert [t for t, _ in b.today_channel.sent] and len(b.today_channel.sent) == 1
