"""Offline checks of the Discord layer: no network, no token. Builds button views only."""
import asyncio
import re

import pytest

discord = pytest.importorskip("discord")

from dispatch import bot, rules  # noqa: E402


def test_views_follow_the_ladder_and_custom_ids(service):
    fresh = service.add("fresh", area="School")
    worn = service.add("worn", area="School")
    service.snooze(worn.id, "plus2h")
    worn = service.snooze(worn.id, "tonight")

    async def build():
        return bot.build_view([fresh, worn])

    view = asyncio.run(build())
    ids = [c.custom_id for c in view.children]
    assert ids[:5] == [f"{a}:{fresh.id}" for a in ("done", "plus2h", "tonight", "tomorrow", "kill")]
    assert ids[5:] == [f"donow:{worn.id}", f"pickday:{worn.id}", f"kill:{worn.id}"]
    assert all(re.fullmatch(bot.TaskButton.__discord_ui_compiled_template__, i) for i in ids)
    assert view.timeout is None                       # persistent


def test_bot_constructs_without_connecting(service, clock, tmp_path):
    from dispatch.config import Settings
    s = Settings(discord_token="not-a-real-token", guild_id=1, max_user_id=2,
                 heartbeat_path=tmp_path / "hb.json")
    assert "not-a-real-token" not in repr(s)

    async def make():
        return bot.DispatchBot(s, service, clock)

    b = asyncio.run(make())
    assert b.intents.message_content is True
    assert rules.snooze_options(0)


def test_undo_button_template_and_registration(service, clock, tmp_path):
    from dispatch.config import Settings
    assert re.fullmatch(bot.UndoButton.__discord_ui_compiled_template__, "undo:42")
    assert not re.fullmatch(bot.UndoButton.__discord_ui_compiled_template__, "done:42")
    assert not re.fullmatch(bot.TaskButton.__discord_ui_compiled_template__, "undo:42")
    s = Settings(discord_token="x", guild_id=1, max_user_id=2, heartbeat_path=tmp_path / "hb.json")

    async def go():
        b = bot.DispatchBot(s, service, clock)
        view = bot.undo_view(42)
        registered = []
        b.add_dynamic_items = lambda *items: registered.extend(items)
        b.loop = asyncio.get_running_loop()
        b.minute_loop = lambda: asyncio.sleep(0)        # don't start the real loop
        await b.setup_hook()
        return view, registered

    view, registered = asyncio.run(go())
    assert [c.custom_id for c in view.children] == ["undo:42"] and view.timeout is None
    assert bot.UndoButton in registered and bot.TaskButton in registered
    assert bot.undo_view(None) is None
