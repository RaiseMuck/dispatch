"""Command legend: '#commands' card + 'help' reply that never asks the AI."""
import asyncio

import pytest

from dispatch import chat, legend


async def _inline(fn, *a, **kw):
    return fn(*a, **kw)


def _router(service):
    def ask(prompt):
        raise AssertionError("help must not call the local AI")
    return chat.Router(service, ask=ask, guess=lambda t, n: None, run=_inline)


@pytest.mark.parametrize("text", ["help", "Help", "HELP?", " commands ", "command", "cmds",
                                  "legend", "?", "help!"])
def test_help_words(text):
    assert legend.is_help(text)


@pytest.mark.parametrize("text", ["help me move #3 to Friday", "add: help Sam move",
                                  "helpful", "done #3", ""])
def test_not_help(text):
    assert not legend.is_help(text)


def test_help_reply_without_ai_and_within_discord_limit(service):
    r = asyncio.run(_router(service).handle("help"))
    assert r.action == "help"
    assert r.text == legend.help_text()
    assert len(r.text) <= chat.MAX_REPLY
    assert "add: call Sam tomorrow 3pm" in r.text and "undo that" in r.text
    assert service.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_add_help_is_still_an_add(service):
    r = asyncio.run(_router(service).handle("add: help Sam move"))
    assert r.action == "add"


def test_embed_fits_discord_limits():
    e = legend.embed_dict()
    assert len(e["fields"]) <= 25 and len(e["description"]) <= 4096
    assert all(len(f["name"]) <= 256 and len(f["value"]) <= 1024 for f in e["fields"])
    total = len(e["title"]) + len(e["description"]) + sum(
        len(f["name"]) + len(f["value"]) for f in e["fields"])
    assert total <= 6000
