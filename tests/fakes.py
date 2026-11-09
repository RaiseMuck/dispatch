"""Tiny stand-ins for discord.py objects (no network, no token) used by the bot tests."""
import itertools

from dispatch import chat, qwen

MAX_ID, OTHER_ID, WEBHOOK_ID = 111, 222, 999
_ids = itertools.count(10_000)

GUESS = qwen.Guess(area="School", payoff=4, likelihood=0.8, estimate_minutes=20,
                   guessed=["area", "payoff", "likelihood", "estimate_minutes"], model="fake", ms=2)


class User:
    def __init__(self, uid, bot=False):
        self.id, self.bot = uid, bot


class _Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class Sent:
    def __init__(self):
        self.id = next(_ids)


class Channel:
    def __init__(self, cid, name, history=()):
        self.id, self.name = cid, name
        self.sent = []
        self._history = list(history)

    def typing(self):
        return _Typing()

    async def send(self, text, **kw):
        self.sent.append((text, kw))
        return Sent()

    async def history(self, limit=100, after=None, oldest_first=False):
        msgs = sorted(self._history, key=lambda m: m.id, reverse=not oldest_first)
        if after is not None:
            msgs = [m for m in msgs if m.id > after.id]
        for m in msgs[:limit]:
            yield m


class Message:
    def __init__(self, channel, author, content, webhook_id=None, reference=None, mid=None):
        self.id = mid or next(_ids)
        self.channel, self.author, self.content = channel, author, content
        self.webhook_id, self.reference = webhook_id, reference
        self.replies = []

    async def reply(self, text, **kw):
        self.replies.append((text, kw))
        return Sent()


async def inline(fn, *a):
    return fn(*a)


def router(service, answers=None, raise_for=(), propose=None, needs=None, guess=None):
    answers = answers or {}

    def ask(prompt):
        msg = prompt.rsplit('Message: "', 1)[1].rsplit('"', 1)[0]
        if msg in raise_for:
            raise RuntimeError("ollama down")
        return answers.get(msg, {"action": "unclear"})

    return chat.Router(service, ask=ask, guess=guess or (lambda t, n: GUESS), run=inline,
                       propose=propose, needs=needs)


class _Response:
    def __init__(self, log):
        self.log = log

    async def send_message(self, text=None, **kw):
        self.log.append(("send", text, kw))

    async def send_modal(self, modal):
        self.log.append(("modal", modal, {}))

    async def defer(self, **kw):
        self.log.append(("defer", None, kw))


class _Followup:
    def __init__(self, log):
        self.log = log

    async def send(self, text=None, **kw):
        self.log.append(("followup", text, kw))


class Interaction:
    """A button press / modal submit: records every answer in order (defer before followup)."""

    def __init__(self, user_id=MAX_ID):
        self.user = User(user_id)
        self.log = []
        self.response = _Response(self.log)
        self.followup = _Followup(self.log)

    def texts(self):
        return [t for kind, t, _ in self.log if kind in ("send", "followup")]

    def last_view_ids(self):
        for kind, _t, kw in reversed(self.log):
            if "view" in kw:
                return [c.custom_id for c in kw["view"].children]
        return []
