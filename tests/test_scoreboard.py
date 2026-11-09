"""#scoreboard: picks, streak, time, EV captured %, EV per hour - counts only, never titles."""
import json
from datetime import date, datetime

from dispatch import scoreboard
from dispatch.clock import ET


def at(clock, d, h, m=0):
    clock.set(datetime(2026, 10, d, h, m, tzinfo=ET))


def post(service, clock, d, tasks):
    at(clock, d, 8)
    for i, t in enumerate(tasks, 1):
        service.log(t.id, "morning_post", actor="scheduler", rank=i, score=1.0, why="x")


def test_week_starts_on_sunday():
    assert scoreboard.week_start(date(2026, 10, 4)) == date(2026, 10, 4)    # Sun
    assert scoreboard.week_start(date(2026, 10, 10)) == date(2026, 10, 4)   # Sat
    assert scoreboard.week_start(date(2026, 10, 11)) == date(2026, 10, 11)


def test_ev_time_and_picks(service, clock):
    at(clock, 4, 7)                                        # Sunday Oct 4
    a = service.add("secret alpha", area="School", status="queued", payoff=5, likelihood=1.0,
                    estimate_minutes=60)
    b = service.add("secret beta", area="School", status="queued", payoff=4, likelihood=0.5,
                    estimate_minutes=30, guessed=["estimate"])
    c = service.add("secret gamma", area="School", status="queued", payoff=2, likelihood=0.5,
                    estimate_minutes=20)
    k = service.add("secret killed", area="School", status="queued", payoff=5, likelihood=1.0)
    post(service, clock, 5, [a, b, c])                     # Mon three
    at(clock, 5, 10)
    service.mark_done(a.id, actor="button")
    at(clock, 5, 11)
    service.mark_done(b.id, actor="button")
    post(service, clock, 6, [k])
    at(clock, 6, 9)
    service.kill(k.id, actor="button")                     # dropped on purpose: not a miss
    at(clock, 7, 12)
    d = scoreboard.compute(service.conn, clock.now())
    w = d["this_week"]
    assert w["done"] == 2 and w["minutes"] == 90 and w["minutes_set_by_max"] == 1
    assert w["ev_done"] == 7.0                             # 5x1.0 + 4x0.5
    assert w["ev_missed"] == 1.0                           # c: 2x0.5, posted and still open
    assert w["ev_pct"] == 0.875
    assert w["ev_per_hour"] == round(7.0 / 1.5, 2)
    assert (w["picks_hits"], w["picks_posted"]) == (2, 4)
    assert d["minutes_all_time"] == 90


def test_undone_done_does_not_count_and_late_done_is_a_miss_for_that_week(service, clock):
    at(clock, 4, 7)
    a = service.add("late one", area="School", status="queued", payoff=3, likelihood=1.0,
                    estimate_minutes=30, due_at=datetime(2026, 10, 9, 17, 0, tzinfo=ET))
    b = service.add("undone", area="School", status="queued", payoff=2, likelihood=1.0,
                    estimate_minutes=30)
    post(service, clock, 5, [b])
    at(clock, 5, 12)
    service.mark_done(b.id, actor="button")
    service.undo(service.last_event_id)                    # the done never happened
    at(clock, 12, 9)                                       # following Monday
    service.mark_done(a.id, actor="button")
    d = scoreboard.compute(service.conn, clock.now())
    lw = d["last_week"]
    assert lw["done"] == 0 and lw["ev_done"] == 0
    assert lw["ev_missed"] == 5.0                          # a (due Fri, done late) + b (posted, open)
    assert lw["ev_pct"] == 0.0 and lw["ev_per_hour"] is None
    assert d["this_week"]["ev_done"] == 3.0 and d["this_week"]["ev_pct"] == 1.0


def test_embed_has_no_titles_and_fits_discord_limits(service, clock):
    at(clock, 4, 7)
    t = service.add("very private title", area="School", status="queued")
    post(service, clock, 5, [t])
    at(clock, 5, 10)
    service.mark_done(t.id, actor="button")
    data = scoreboard.compute(service.conn, clock.now())
    e = scoreboard.embed_dict(data, stamp="10:00 AM")
    text = json.dumps(e, ensure_ascii=False)
    assert "private" not in text
    assert len(e["fields"]) <= 25 and len(e["title"]) <= 256
    assert all(len(f["name"]) <= 256 and len(f["value"]) <= 1024 for f in e["fields"])
    total = len(e["title"]) + len(e["footer"]["text"]) + sum(
        len(f["name"]) + len(f["value"]) for f in e["fields"])
    assert total <= 6000
    assert "Updated 10:00 AM" in e["footer"]["text"]
    # the bot only edits when something changed: same data -> same body without the stamp
    assert scoreboard.embed_dict(data) == scoreboard.embed_dict(
        scoreboard.compute(service.conn, clock.now()))


def test_empty_database_renders(service, clock):
    e = scoreboard.embed_dict(scoreboard.compute(service.conn, clock.now()))
    values = {f["name"]: f["value"] for f in e["fields"]}
    assert values["Picks this week"] == "no data yet"
    assert values["Streak"] == "0 days"
    assert values["EV per hour"].startswith("—")


def test_bot_posts_once_then_edits_only_on_change(service, clock, tmp_path):
    import asyncio

    import pytest
    discord = pytest.importorskip("discord")
    from dispatch import bot
    from dispatch.config import Settings

    class Msg:
        def __init__(self, mid, embed):
            self.id, self.embed, self.edits = mid, embed, 0

        async def edit(self, content=None, embed=None):
            self.embed, self.edits = embed, self.edits + 1

    class Board:
        name = "scoreboard"

        def __init__(self):
            self.msgs = {}

        async def send(self, embed=None, **kw):
            m = Msg(1000 + len(self.msgs), embed)
            self.msgs[m.id] = m
            return m

        async def fetch_message(self, mid):
            if mid not in self.msgs:
                raise discord.NotFound(type("R", (), {"status": 404, "reason": "x"})(), "gone")
            return self.msgs[mid]

    async def run():
        b = bot.DispatchBot(Settings(discord_token="x", guild_id=1, max_user_id=2,
                                     db_path=tmp_path / "test.db"), service, clock)
        ch = Board()
        b.scoreboard_channel = ch
        await b.update_scoreboard()
        await b.update_scoreboard()                        # nothing changed: no edit
        (first,) = ch.msgs.values()
        assert first.edits == 0 and first.embed.title == "Dispatch scoreboard"
        t = service.add("x", area="School", status="queued")
        service.mark_done(t.id, actor="button")
        await b.update_scoreboard()                        # a done: same message, edited
        assert len(ch.msgs) == 1 and first.edits == 1
        ch.msgs.clear()                                    # Max deleted it: post a new one
        service.mark_done(service.add("y", area="School", status="queued").id, actor="button")
        await b.update_scoreboard()
        assert len(ch.msgs) == 1

    asyncio.run(run())
