"""Phase 2.1 Discord layer, offline (fake channels and interactions, no network, no token):
button layout (item rows + swap row + mode row), mode buttons, the swap flow, the waiting box,
Got it / Wait more / Start / Skip / Split, and non-Max presses."""
import asyncio
import re
from datetime import datetime, timedelta

import pytest

discord = pytest.importorskip("discord")

import fakes  # noqa: E402
from dispatch import bot, rules  # noqa: E402
from dispatch.clock import ET  # noqa: E402
from dispatch.config import Settings  # noqa: E402
from dispatch.scheduler import Message  # noqa: E402

NOW = datetime(2026, 10, 5, 9, 0, tzinfo=ET)


def make_bot(service, clock, tmp_path, propose=None):
    s = Settings(discord_token="x", guild_id=5, max_user_id=fakes.MAX_ID,
                 heartbeat_path=tmp_path / "hb.json", inbox_webhook_id=fakes.WEBHOOK_ID)
    b = bot.DispatchBot(s, service, clock, router=fakes.router(service, {}, propose=propose))
    b.inbox_channel = fakes.Channel(1, "inbox")
    b.today_channel = fakes.Channel(2, "today")
    return b


def run(coro_fn):
    async def go():
        return await coro_fn()
    return asyncio.run(go())


@pytest.fixture
def day(service, clock):
    clock.set(NOW)
    ids = {}
    for key, place, mins in [("big", "home", 90), ("call", "phone", 20), ("errand", "out", 30),
                             ("short", "home", 15), ("focus", "home", 60)]:
        ids[key] = service.add(key, area="Personal admin", place=place, energy="light",
                               estimate_minutes=mins).id
    return ids


def rows_of(view):
    out = {}
    for c in view.children:
        out.setdefault(c.row, []).append(c.custom_id)
    return out


def test_morning_view_is_three_items_then_swaps_then_modes(service, day):
    active = service.morning_items()
    msg = Message("morning", "Good morning", active, mode_row=True)
    view = run(lambda: asyncio.sleep(0, bot.build_view(active, msg, mode_row=True)))
    rows = rows_of(view)
    assert len(rows) == 5 and len(view.children) <= 25
    for i, t in enumerate(active):
        assert rows[i][0] == f"done:{t.id}"
    assert rows[3] == [f"swap:{t.id}" for t in active]
    assert rows[4] == ["mode:sick", "mode:slammed", "mode:normal"]
    assert all(re.fullmatch(bot.ModeButton.__discord_ui_compiled_template__, i) for i in rows[4])
    assert all(re.fullmatch(bot.SwapButton.__discord_ui_compiled_template__, i) for i in rows[3])


def test_nag_view_swaps_only_actives_and_flagged_items_get_no_snooze(service, day, clock):
    service.conn.execute("UPDATE tasks SET due_at=?, next_nag_at=? WHERE id=?",
                         (NOW.isoformat(), NOW.isoformat(), day["short"]))
    service.conn.commit()
    items = service.by_status("active") + [service.get(day["short"])]
    msg = Message("nag", "4 items", items, flags={day["short"]: "can't fit today (x) - pick a day?"})
    view = run(lambda: asyncio.sleep(0, bot.build_view(items, msg)))
    rows = rows_of(view)
    assert rows[3] == [f"donow:{day['short']}", f"pickday:{day['short']}", f"kill:{day['short']}"]
    assert rows[4] == [f"swap:{t.id}" for t in items[:3]]


def test_send_batch_chunks_three_with_the_mode_row_and_four_without(service, day, clock,
                                                                   tmp_path):
    b = make_bot(service, clock, tmp_path)
    items = service.by_status("active", "queued")                    # 5 items
    run(lambda: b.send_batch(Message("morning", "Good morning", items, mode_row=True)))
    sent = b.today_channel.sent
    assert len(sent) == 2 and sent[1][0] == "(continued)"
    first = [c.custom_id for c in sent[0][1]["view"].children]
    assert first[-3:] == ["mode:sick", "mode:slammed", "mode:normal"]
    b.today_channel.sent.clear()
    run(lambda: b.send_batch(Message("nag", "5 waiting", items)))
    assert len(b.today_channel.sent) == 2
    assert not any(c.custom_id.startswith("mode:")
                   for c in b.today_channel.sent[0][1]["view"].children)


def test_mode_button_defers_then_answers_with_undo(service, day, clock, tmp_path):
    b = make_bot(service, clock, tmp_path)
    it = fakes.Interaction()
    run(lambda: b.handle_mode(it, "sick"))
    assert [k for k, _, _ in it.log] == ["defer", "followup"]
    assert it.texts()[0].startswith("Sick/home day:")
    assert it.last_view_ids()[0].startswith("undo:")
    assert service.current_mode() == rules.SICK


def test_swap_flow_reason_menu_then_confirmation_with_undo(service, day, clock, tmp_path):
    b = make_bot(service, clock, tmp_path)
    it = fakes.Interaction()
    run(lambda: b.handle_swap(it, day["big"]))
    kind, text, kw = it.log[0]
    assert kind == "send" and kw.get("ephemeral") and text.startswith(f"Why swap #{day['big']}")
    assert it.last_view_ids() == [f"swapwhy:{r}:{day['big']}" for r in rules.SWAP_REASONS]
    it2 = fakes.Interaction()
    run(lambda: b.handle_swap_reason(it2, "long", day["big"]))
    assert it2.log[0][0] == "send" and not it2.log[0][2].get("ephemeral")
    assert it2.texts()[0].startswith(f"Swapped out #{day['big']} big (too long). In: #")
    assert it2.last_view_ids()[-1].startswith("undo:")


def test_waiting_reason_opens_the_box_with_a_default(service, day, clock, tmp_path):
    b = make_bot(service, clock, tmp_path)
    it = fakes.Interaction()
    run(lambda: b.handle_swap_reason(it, "waiting", day["call"]))
    kind, modal, _ = it.log[0]
    assert kind == "modal" and modal.when.default == "in 3 days"
    it2 = fakes.Interaction()
    run(lambda: b.handle_wait_until(it2, day["call"], "banana"))
    assert it2.log[0][2].get("ephemeral") and "Couldn't read" in it2.texts()[0]
    run(lambda: b.handle_wait_until(it2, day["call"], "in 3 days"))
    assert service.get(day["call"]).status == "waiting"
    assert "check in Thu 10/8" in it2.texts()[-1]


def test_third_swap_forces_a_decision(service, day, clock, tmp_path):
    b = make_bot(service, clock, tmp_path)
    service.conn.execute("UPDATE tasks SET swap_count=2 WHERE id=?", (day["big"],))
    service.conn.commit()
    it = fakes.Interaction()
    run(lambda: b.handle_swap_reason(it, "out", day["big"]))
    assert "swapped twice already" in it.texts()[0]
    assert it.last_view_ids() == [f"donow:{day['big']}", f"pickday:{day['big']}",
                                  f"kill:{day['big']}", f"splitask:{day['big']}"]


def test_second_too_long_defers_and_offers_a_split(service, day, clock, tmp_path):
    b = make_bot(service, clock, tmp_path, propose=lambda t, n, m: ("Clear the desk", 10))
    run(lambda: b.handle_swap_reason(fakes.Interaction(), "long", day["big"]))
    service.start(day["big"], actor="button")
    clock.advance(timedelta(days=1))
    it = fakes.Interaction()
    run(lambda: b.handle_swap_reason(it, "long", day["big"]))
    assert [k for k, _, _ in it.log] == ["defer", "followup"]
    assert 'Split it? First step: "Clear the desk" (~10 min).' in it.texts()[0]
    assert it.last_view_ids() == [f"split:{day['big']}", f"splitno:{day['big']}",
                                  it.last_view_ids()[-1]]
    it2 = fakes.Interaction()
    run(lambda: b.handle_button(it2, "split", day["big"]))
    kids = service.conn.execute("SELECT id FROM tasks WHERE parent_id=?", (day["big"],)).fetchall()
    assert len(kids) == 1 and it2.texts()[0].startswith(f"Split: #{kids[0][0]} Clear the desk")


def test_got_it_wait_more_start_skip_buttons(service, day, clock, tmp_path):
    b = make_bot(service, clock, tmp_path)
    service.set_waiting(day["call"], actor="chat")
    it = fakes.Interaction()
    run(lambda: b.handle_button(it, "waitmore", day["call"]))
    assert "check in on #%d again" % day["call"] in it.texts()[0]
    run(lambda: b.handle_button(it, "gotit", day["call"]))
    assert service.get(day["call"]).status in ("queued", "active")
    # Start / Skip from a minutes suggestion
    r = run(lambda: b.router.handle("I've got 20 minutes"))
    assert r.action == "unclear"                                     # model said nothing
    r = b.router._minutes({"minutes": 20}, "I've got 20 minutes")
    first = int(r.buttons[0][1].split(":")[1])
    it3 = fakes.Interaction()
    run(lambda: b.handle_button(it3, "skip", first))
    assert it3.texts()[0] != r.text
    it4 = fakes.Interaction()
    run(lambda: b.handle_button(it4, "start", first))
    assert it4.texts()[0].startswith(f"Go: #{first}") and it4.last_view_ids()[-1].startswith("undo:")


def test_non_max_presses_are_refused(service, day, clock, tmp_path):
    b = make_bot(service, clock, tmp_path)
    for fn in (lambda it: b.handle_mode(it, "sick"), lambda it: b.handle_swap(it, day["big"]),
               lambda it: b.handle_swap_reason(it, "long", day["big"]),
               lambda it: b.handle_button(it, "start", day["big"])):
        it = fakes.Interaction(user_id=fakes.OTHER_ID)
        run(lambda: fn(it))
        assert it.texts() == ["Only Max can use these buttons."]
    assert service.current_mode() is None and service.get(day["big"]).status == "active"


def test_new_items_are_registered_and_templates_are_distinct(service, clock, tmp_path):
    s = Settings(discord_token="x", guild_id=1, max_user_id=2, heartbeat_path=tmp_path / "hb.json")

    async def go():
        b = bot.DispatchBot(s, service, clock)
        registered = []
        b.add_dynamic_items = lambda *items: registered.extend(items)
        b.loop = asyncio.get_running_loop()
        b.minute_loop = lambda: asyncio.sleep(0)
        await b.setup_hook()
        return registered

    reg = run(go)
    assert {bot.ModeButton, bot.SwapButton, bot.SwapReasonButton} <= set(reg)
    tt = bot.TaskButton.__discord_ui_compiled_template__
    for cid in ("gotit:3", "waitmore:3", "start:3", "skip:3", "split:3", "splitno:3", "splitask:3"):
        assert re.fullmatch(tt, cid)
    for cid in ("swap:3", "mode:sick", "swapwhy:long:3"):
        assert not re.fullmatch(tt, cid)
    assert not re.fullmatch(bot.SwapButton.__discord_ui_compiled_template__, "swapwhy:long:3")
