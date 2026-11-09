"""Siri shortcut via a Discord webhook on #inbox: accepted ONLY from INBOX_WEBHOOK_ID, always a
capture (add, or idea/park), one reply each, rate-limited to 20 adds an hour."""
import asyncio
from datetime import datetime, timedelta

import pytest

discord = pytest.importorskip("discord")

import fakes  # noqa: E402
from dispatch import bot as botmod  # noqa: E402
from dispatch import config  # noqa: E402
from dispatch.clock import ET  # noqa: E402
from dispatch.config import Settings  # noqa: E402

NOW = datetime(2026, 10, 5, 9, 0, tzinfo=ET)
HOOK = fakes.User(4321, bot=True)          # webhook messages are "bot" authored


def run_siri(service, clock, tmp_path, texts, webhook_setting=fakes.WEBHOOK_ID,
             webhook_id=fakes.WEBHOOK_ID, gap=timedelta(0)):
    clock.set(clock.now() if clock.now() > NOW else NOW)

    async def go():
        s = Settings(discord_token="x", guild_id=5, max_user_id=fakes.MAX_ID,
                     heartbeat_path=tmp_path / "hb.json", inbox_webhook_id=webhook_setting)
        b = botmod.DispatchBot(s, service, clock, router=fakes.router(service))
        b.inbox_channel = fakes.Channel(1, "inbox")
        b.today_channel = fakes.Channel(2, "today")
        out = []
        for text in texts:
            m = fakes.Message(b.inbox_channel, HOOK, text, webhook_id=webhook_id)
            await b.on_message(m)
            out.append(m.replies)
            clock.advance(gap)
        return out

    return asyncio.run(go())


def test_webhook_message_is_added_with_actor_siri_and_undo(service, clock, tmp_path):
    [replies] = run_siri(service, clock, tmp_path, ["call Sam tomorrow at 3pm"])
    assert len(replies) == 1
    body, kw = replies[0]
    t = service.open_tasks()[0]
    assert t.title == "call Sam" and t.due_at == datetime(2026, 10, 6, 15, 0, tzinfo=ET)
    assert body.startswith("Added #1 call Sam") and "Guessed: payoff 4/5" in body
    ev = service.conn.execute("SELECT id, actor FROM events WHERE kind='created'").fetchone()
    assert ev["actor"] == "siri" and [c.custom_id for c in kw["view"].children] == [f"undo:{ev['id']}"]


def test_wrong_webhook_or_feature_off_is_ignored(service, clock, tmp_path):
    assert run_siri(service, clock, tmp_path, ["add: x"], webhook_id=12345) == [[]]
    assert run_siri(service, clock, tmp_path, ["add: x"], webhook_setting=None) == [[]]
    assert service.open_tasks() == []


@pytest.mark.parametrize("text,title,status", [
    ("add: renew permit tomorrow 3pm", "renew permit", "active"),
    ("pick up dry cleaning", "pick up dry cleaning", "active"),
    ("Idea, restaurant analytics", "restaurant analytics", "parked"),
    ("idea: fantasy highlight app", "fantasy highlight app", "parked"),
    ("park learn Blender", "learn Blender", "parked"),
])
def test_capture_kinds(service, clock, tmp_path, text, title, status):
    run_siri(service, clock, tmp_path, [text])
    [t] = service.open_tasks()
    assert (t.title, t.status) == (title, status)


def test_empty_dictation_and_long_text(service, clock, tmp_path):
    out = run_siri(service, clock, tmp_path, ["   ", "idea", "x" * 400])
    assert out[0][0][0] == "I heard nothing - try again."
    assert out[1][0][0] == "I heard nothing - try again."
    [t] = service.open_tasks()
    assert len(t.title) == 300 and t.title.endswith("...")


def test_rate_limit_20_per_hour(service, clock, tmp_path):
    out = run_siri(service, clock, tmp_path, [f"task {i}" for i in range(21)], gap=timedelta(minutes=1))
    assert all(len(r) == 1 for r in out)
    assert out[20][0][0].startswith("Too many Siri adds this hour - paused until")
    n = len(service.open_tasks())
    assert n == 20
    clock.advance(timedelta(minutes=41))                 # 61 min after the first add
    run_siri(service, clock, tmp_path, ["one more"])
    assert len(service.open_tasks()) == 21


def test_settings_parse_webhook_id(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("DISCORD_TOKEN", "fake-test-value")
    monkeypatch.setenv("DISCORD_GUILD_ID", "123456789012345678")
    monkeypatch.setenv("MAX_USER_ID", "876543210987654321")
    monkeypatch.setenv("INBOX_WEBHOOK_ID", "abc")
    monkeypatch.setenv("CANVAS_ICS_URL", "")
    s = config.load_settings(tmp_path / "none.env")
    assert s.inbox_webhook_id is None and s.problems() == [] and "siri=off" in repr(s)
    monkeypatch.setenv("INBOX_WEBHOOK_ID", "1300000000000000001")
    s = config.load_settings(tmp_path / "none.env")
    assert s.inbox_webhook_id == 1300000000000000001 and "siri=on" in repr(s)
