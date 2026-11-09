"""No silent-drop paths: Qwen never parks, park:/idea: do, the Sunday review shows everything
waiting, undated queued items reach the morning three, and bad .env IDs stop the bot."""
from datetime import datetime, timedelta

import pytest

from dispatch import scheduler
from dispatch.areas import AREAS
from dispatch.clock import ET
from dispatch.scheduler import PrintPoster, RunState, tick

FAR_FUTURE = datetime(2030, 1, 1, tzinfo=ET)


def run(service, clock, minutes):
    poster = PrintPoster(clock, out=lambda *_: None)
    for _ in range(minutes):
        tick(service, poster, clock, RunState())
        clock.advance(timedelta(minutes=1))
    return poster.sent


# ---- (a) parking only on an explicit prefix -------------------------------------------------
def test_qwen_guessing_side_venture_never_parks(service):
    guess = lambda _title: "Side-venture ideas"        # noqa: E731 - Qwen's (wrong) guess
    first = service.capture("add: call Sam about the pilot tomorrow 3pm", area_picker=guess)
    assert first.area == "Side-venture ideas" and first.status == "active"
    for i in range(2):
        service.capture(f"add: thing {i}", area_picker=guess)
    fourth = service.capture("add: one more", area_picker=guess)
    assert fourth.status == "queued"                   # cap rule, not parked
    assert service.by_status("parked") == []


@pytest.mark.parametrize("text,title", [
    ("park: learn Blender", "learn Blender"),
    ("idea: AI menu pricing for restaurants", "AI menu pricing for restaurants"),
    ("IDEA:  fantasy highlight reel by Friday", "fantasy highlight reel"),
])
def test_park_and_idea_prefixes_park(service, text, title):
    t = service.capture(text, area_picker=lambda _t: "Side-venture ideas")
    assert (t.title, t.status, t.next_nag_at) == (title, "parked", None)
    assert service.active_count() == 0
    assert service.due_batch(FAR_FUTURE) == []         # parked items never nag...


# ---- (b) the Sunday weekly review ----------------------------------------------------------
def test_weekly_review_sunday_8am_lists_every_queued_and_parked_item(service, clock):
    clock.set(datetime(2026, 10, 3, 7, 0, tzinfo=ET))  # Saturday
    for i in range(3):
        service.add(f"active {i}", area="School")
    q1 = service.add("queued portfolio", area="Portfolio")
    q2 = service.add("queued mystery", area=None)      # unsorted
    q3 = service.add("queued coreflow", area="Coreflow/CLARA",
                     due_at=datetime(2026, 10, 20, 9, 0, tzinfo=ET))
    p1 = service.capture("idea: restaurant analytics", area_picker=lambda _t: "Side-venture ideas")
    sent = run(service, clock, 26 * 60 + 1)            # Sat 07:00 .. Sun 09:00
    weekly = [(w, m) for w, m in sent if m.kind == "weekly"]
    assert len(weekly) == 1
    when, msg = weekly[0]
    assert when == datetime(2026, 10, 4, 8, 0, tzinfo=ET) and when.weekday() == 6
    assert {t.id for t in msg.listed} == {q1.id, q2.id, q3.id, p1.id}
    text = msg.render()
    headers = [ln.strip("*") for ln in msg.body if ln.startswith("**")]
    assert headers == ["Coreflow/CLARA", "Side-venture ideas", "Portfolio", "unsorted"]  # area order
    assert headers[:3] == [a for a in AREAS if a in headers]
    assert "restaurant analytics [parked]" in text and "queued mystery" in text
    assert "(due Tue 10/20 9:00 AM)" in text


def test_weekly_review_catches_up_if_the_pc_was_off_on_sunday(service, clock):
    clock.set(datetime(2026, 10, 4, 7, 0, tzinfo=ET))  # Sunday
    service.add("waiting", area="School", status="queued")
    assert [m.kind for _, m in run(service, clock, 90) if m.kind == "weekly"] == ["weekly"]
    clock.set(datetime(2026, 10, 12, 7, 0, tzinfo=ET))  # PC off all of Sun 10/11; back Monday
    sent = run(service, clock, 90)
    weekly = [w for w, m in sent if m.kind == "weekly"]
    assert weekly == [datetime(2026, 10, 12, 8, 0, tzinfo=ET)]
    assert service.meta_get(scheduler.META_WEEKLY) == "2026-10-12"


def test_undated_queued_item_reaches_the_morning_three(service, clock):
    clock.set(datetime(2026, 9, 28, 7, 0, tzinfo=ET))
    a = [service.add(f"active {i}", area="Coreflow/CLARA") for i in range(3)]
    undated = service.add("undated, waiting", area="Home & life")
    dated = service.add("dated, waiting", area="School", due_at=datetime(2026, 10, 5, 9, 0, tzinfo=ET))
    assert (undated.status, dated.status) == ("queued", "queued")
    _, promoted = service.mark_done(a[0].id)
    assert [p.id for p in promoted] == [dated.id]      # promotion order: dated first...
    _, promoted = service.kill(a[1].id)
    assert [p.id for p in promoted] == [undated.id]    # ...then the undated one
    clock.set(datetime(2026, 9, 29, 7, 59, tzinfo=ET))
    sent = run(service, clock, 2)
    morning = [m for _, m in sent if m.kind == "morning"][0]
    assert undated.id in [t.id for t in morning.items] and len(morning.items) == 3


# ---- 3. MAX_USER_ID must be a real number before the bot starts ---------------------------
@pytest.mark.parametrize("bad", ["", "   ", "abc", "12a45", "@max"])
def test_run_refuses_blank_or_non_numeric_max_user_id(monkeypatch, tmp_path, capsys, bad):
    import dispatch.config as config
    from dispatch.__main__ import main
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)          # no real .env is read
    monkeypatch.setenv("DISCORD_TOKEN", "fake-test-value")
    monkeypatch.setenv("DISCORD_GUILD_ID", "123456789012345678")
    monkeypatch.setenv("MAX_USER_ID", bad)
    assert main(["run"]) == 2
    out = capsys.readouterr().out
    assert "Can't start yet" in out and "MAX_USER_ID" in out
    assert "fake-test-value" not in out                            # token never echoed


def test_valid_ids_pass_the_check(monkeypatch, tmp_path):
    import dispatch.config as config
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("DISCORD_TOKEN", "fake-test-value")
    monkeypatch.setenv("DISCORD_GUILD_ID", "123456789012345678")
    monkeypatch.setenv("MAX_USER_ID", "876543210987654321")
    assert config.load_settings().problems() == []
