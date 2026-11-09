"""Canvas calendar feed: parsing, sync (add / move / never resurrect / skip past), cadence,
failure notes (at most one a day, never in quiet hours), and the link never appears in logs."""
import asyncio
import logging
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from dispatch import canvas, db, qwen, rules
from dispatch.clock import ET
from dispatch.scheduler import PrintPoster

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "canvas-sample.ics"
TEXT = FIXTURE.read_text(encoding="utf-8")
NOW = datetime(2026, 10, 5, 9, 0, tzinfo=ET)
SECRET = "https://feed.example.invalid/feeds/calendars/user_NOT-A-REAL-LINK.ics"   # fake
G = qwen.Guess(area="Fantasy", payoff=4, likelihood=0.9, estimate_minutes=60,
               guessed=["area", "payoff", "likelihood", "estimate_minutes"], model="fake", ms=1)


def by_uid(events):
    return {e.uid.split("@")[0]: e for e in events}


def test_parser_handles_the_canvas_shapes():
    ev = by_uid(canvas.parse_ics(TEXT))
    assert set(ev) == {"event-assignment-1001", "event-assignment-1002", "event-calendar-event-2001",
                       "event-assignment-1003", "event-assignment-0999", "event-assignment-1005"}
    q3 = ev["event-assignment-1001"]
    assert q3.summary == "Quiz 3: Options Greeks, delta and gamma [BUSFIN 4229]"     # \, unescaped
    assert q3.due == datetime(2026, 10, 16, 23, 59, tzinfo=ET)                        # Z -> ET
    assert q3.description == "Covers chapters 10-12.\nOpen book, 45 minutes."
    assert q3.url.endswith("/assignments/1001")
    memo = ev["event-assignment-1002"]
    assert memo.summary.endswith("before the full-group edit session [BUSMHR 4490]")  # unfolded
    assert memo.due == datetime(2026, 10, 20, 23, 59, tzinfo=ET)                      # all-day
    assert ev["event-calendar-event-2001"].due == datetime(2026, 10, 12, 14, 0, tzinfo=ET)
    assert ev["event-assignment-1003"].due == datetime(2026, 10, 14, 14, 0, tzinfo=ET)  # Berlin 20:00
    assert ev["event-assignment-1005"].summary == "Quiz 4: Futures pricing [BUSFIN 4229]"  # one per UID


@pytest.mark.parametrize("bad", ["<html><body>Please log in</body></html>", "", "garbage"])
def test_non_calendar_text_is_an_error(bad):
    with pytest.raises(ValueError):
        canvas.parse_ics(bad)


def test_first_sync_adds_future_items_as_school(service, clock):
    clock.set(NOW)
    res = canvas.sync(service, NOW, lambda: TEXT, guess=lambda e: G)
    assert res.error is None and len(res.added) == 5 and res.skipped_past == 1
    for t in res.added:
        assert t.source == "canvas" and t.area == "School" and t.external_ref.startswith("canvas:event-")
        assert t.payoff == 4 and "area" not in t.guessed          # area is always School
    q3 = service.get_by_ref("canvas:event-assignment-1001@osu.instructure.com")
    assert q3.notes.startswith("Canvas: https://example.instructure.com/courses/111/assignments/1001")
    ev = db.events_for(service.conn, q3.id)[0]
    assert ev["actor"] == "canvas" and '"via": "canvas"' in ev["detail"]
    again = canvas.sync(service, NOW + timedelta(hours=2), lambda: TEXT, guess=lambda e: G)
    assert (len(again.added), again.unchanged) == (0, 5)


def moved_text(new_dtstart: str) -> str:
    return TEXT.replace("DTSTART:20261017T035900Z", f"DTSTART:{new_dtstart}")


def test_moved_date_reschedules_and_undo_restores(service, clock):
    clock.set(NOW)
    canvas.sync(service, NOW, lambda: TEXT, guess=lambda e: G)
    q3 = service.get_by_ref("canvas:event-assignment-1001@osu.instructure.com")
    before = tuple(service.conn.execute("SELECT * FROM tasks WHERE id=?", (q3.id,)).fetchone())
    res = canvas.sync(service, NOW, lambda: moved_text("20261019T035900Z"), force=True)
    assert [t.id for t in res.moved] == [q3.id]
    t = service.get(q3.id)
    assert t.due_at == datetime(2026, 10, 18, 23, 59, tzinfo=ET)
    ev = db.events_for(service.conn, q3.id)[-1]
    assert (ev["kind"], ev["actor"]) == ("rescheduled", "canvas")
    service.undo(ev["id"])
    assert tuple(service.conn.execute("SELECT * FROM tasks WHERE id=?", (q3.id,)).fetchone()) == before


def test_done_item_is_never_resurrected(service, clock):
    clock.set(NOW)
    canvas.sync(service, NOW, lambda: TEXT, guess=lambda e: G)
    q3 = service.get_by_ref("canvas:event-assignment-1001@osu.instructure.com")
    service.mark_done(q3.id)
    res = canvas.sync(service, NOW, lambda: moved_text("20261019T035900Z"), force=True)
    assert res.skipped_closed == 1 and res.moved == []
    assert service.get(q3.id).status == "done"


def test_past_only_feed_adds_nothing(service, clock):
    clock.set(datetime(2026, 12, 1, 9, 0, tzinfo=ET))
    res = canvas.sync(service, clock.now(), lambda: TEXT)
    assert res.added == [] and res.skipped_past == 6 and service.open_tasks() == []


def test_far_item_queues_behind_three_and_never_nags_early(service, clock):
    clock.set(NOW)
    for i in range(3):
        service.add(f"active {i}", area="Coreflow/CLARA")
    canvas.sync(service, NOW, lambda: TEXT, guess=lambda e: G)
    q4 = service.get_by_ref("canvas:event-assignment-1005@osu.instructure.com")   # 3 weeks out
    assert q4.status == "queued" and q4.next_nag_at == q4.due_at
    for h in range(0, 24 * 20, 6):
        assert q4.id not in [t.id for t in service.due_batch(NOW + timedelta(hours=h))]


def test_poll_cadence_and_feature_off(service, clock):
    clock.set(NOW)
    assert canvas.poll_due(service, NOW)
    canvas.sync(service, NOW, lambda: TEXT)
    assert not canvas.poll_due(service, NOW + timedelta(minutes=119))
    assert canvas.poll_due(service, NOW + timedelta(minutes=120))
    assert canvas.sync(service, NOW + timedelta(minutes=30), lambda: TEXT) is None

    async def off():
        return await canvas.poll_in_bot(service, "", NOW + timedelta(hours=5))
    service.meta_set(canvas.META_POLL, None)
    assert asyncio.run(off()) is None and service.meta_get(canvas.META_POLL) is None


def test_poll_in_bot_runs_fetch_and_guesses_in_the_runner(service, clock, monkeypatch):
    clock.set(NOW)
    ran = []

    async def runner(fn, *a):
        ran.append(getattr(fn, "__name__", "?"))
        return fn(*a)

    monkeypatch.setattr(canvas, "fetch", lambda url: TEXT)
    monkeypatch.setattr(canvas, "guess_for", lambda e, base_url=None: G)
    res = asyncio.run(canvas.poll_in_bot(service, SECRET, NOW, run=runner))
    assert len(res.added) == 5 and ran == ["<lambda>", "guess_all"]


def _boom():
    raise urllib.error.URLError(f"cannot reach {SECRET}")


def run_minutes(service, clock, poster, minutes, fetcher=_boom):
    """What the bot does every minute: poll when due, then maybe send the failure note."""
    for _ in range(minutes):
        canvas.sync(service, clock.now(), fetcher)
        canvas.flush_note(service, poster, clock.now())
        clock.advance(timedelta(minutes=1))


def test_failures_one_note_a_day_never_in_quiet_hours_link_never_logged(service, clock, caplog):
    caplog.set_level(logging.DEBUG)
    clock.set(datetime(2026, 10, 5, 3, 0, tzinfo=ET))        # first failure at 03:00
    poster = PrintPoster(clock, out=lambda *_: None)
    run_minutes(service, clock, poster, 5 * 60 + 1)          # 03:00 .. 08:00
    notes = [(w, m) for w, m in poster.sent if m.kind == "note"]
    assert [w for w, _ in notes] == [datetime(2026, 10, 5, 8, 0, tzinfo=ET)]
    assert "Canvas feed didn't load today (URLError)" in notes[0][1].header
    assert notes[0][1].items == [] and notes[0][1].mention is False
    run_minutes(service, clock, poster, 12 * 60)             # more failures the same day
    assert len([m for _, m in poster.sent if m.kind == "note"]) == 1
    clock.set(datetime(2026, 10, 6, 9, 0, tzinfo=ET))
    run_minutes(service, clock, poster, 3)                   # next day: one more
    assert len([m for _, m in poster.sent if m.kind == "note"]) == 2
    assert SECRET not in caplog.text and "URLError" in caplog.text
    assert not any(SECRET in str(v) for v in service.conn.execute("SELECT * FROM meta").fetchall()
                   for v in v)
    polls = service.meta_get(canvas.META_POLL)
    assert polls is not None                                 # failures are retried every 2 h


def test_a_working_feed_after_a_failure_sends_no_note(service, clock):
    clock.set(datetime(2026, 10, 5, 3, 0, tzinfo=ET))
    poster = PrintPoster(clock, out=lambda *_: None)
    run_minutes(service, clock, poster, 1)                   # fails at 03:00 (quiet)
    assert service.meta_get(canvas.META_ERR_PENDING) == "URLError"
    run_minutes(service, clock, poster, 6 * 60, fetcher=lambda: TEXT)   # works at 05:00
    assert [m for _, m in poster.sent if m.kind == "note"] == []
    assert len(service.by_status("active", "queued")) == 5


# ---- review r1 required #1: a re-poll never overwrites Max's own date -------------------------
Q3 = "canvas:event-assignment-1001@osu.instructure.com"


def test_max_moves_a_canvas_item_and_the_repoll_leaves_it_alone(service, clock):
    clock.set(NOW)
    canvas.sync(service, NOW, lambda: TEXT, guess=lambda e: G)
    q3 = service.get_by_ref(Q3)
    mine = datetime(2026, 10, 15, 18, 0, tzinfo=ET)            # study before the Fri deadline
    service.reschedule(q3.id, mine, actor="chat")
    service.snooze(q3.id, rules.PLUS2H, explicit=NOW + timedelta(hours=3), actor="chat")
    for h in (2, 4, 6):                                         # three more polls, same feed
        res = canvas.sync(service, NOW + timedelta(hours=h), lambda: TEXT)
        assert res.moved == [] and res.unchanged == 5
    t = service.get(q3.id)
    assert t.due_at == mine and t.snooze_count == 1             # his date and his ladder kept
    kinds = [(r["kind"], r["actor"]) for r in db.events_for(service.conn, q3.id)]
    assert ("rescheduled", "canvas") not in kinds
    # the professor then really moves it: exactly one Canvas move
    res = canvas.sync(service, NOW + timedelta(hours=8), lambda: moved_text("20261019T035900Z"))
    assert [x.id for x in res.moved] == [q3.id]
    assert service.get(q3.id).due_at == datetime(2026, 10, 18, 23, 59, tzinfo=ET)
    res = canvas.sync(service, NOW + timedelta(hours=10), lambda: moved_text("20261019T035900Z"))
    assert res.moved == []                                       # and only once
    kinds = [(r["kind"], r["actor"]) for r in db.events_for(service.conn, q3.id)]
    assert kinds.count(("rescheduled", "canvas")) == 1


def test_undoing_a_canvas_move_sticks(service, clock):
    clock.set(NOW)
    canvas.sync(service, NOW, lambda: TEXT, guess=lambda e: G)
    canvas.sync(service, NOW, lambda: moved_text("20261019T035900Z"), force=True)
    q3 = service.get_by_ref(Q3)
    ev = [r for r in db.events_for(service.conn, q3.id) if r["kind"] == "rescheduled"][-1]
    service.undo(ev["id"], actor="button")
    res = canvas.sync(service, NOW + timedelta(hours=2), lambda: moved_text("20261019T035900Z"))
    assert res.moved == [] and service.get(q3.id).due_at == datetime(2026, 10, 16, 23, 59, tzinfo=ET)


def test_canvas_moves_are_announced_once_with_undo_never_in_quiet_hours(service, clock):
    clock.set(NOW)
    for i in range(3):                                          # the Canvas items wait in the queue
        service.add(f"active {i}", area="Coreflow/CLARA")
    canvas.sync(service, NOW, lambda: TEXT, guess=lambda e: G)
    poster = PrintPoster(clock, out=lambda *_: None)
    night = datetime(2026, 10, 6, 2, 0, tzinfo=ET)
    two = moved_text("20261019T035900Z").replace("DTSTART;VALUE=DATE:20261020", "DTSTART;VALUE=DATE:20261022")
    canvas.sync(service, night, lambda: two, force=True)        # two moves at 2 AM
    assert not canvas.flush_move_notes(service, poster, night)  # quiet hours: wait
    assert canvas.flush_move_notes(service, poster, night.replace(hour=8))
    assert not canvas.flush_move_notes(service, poster, night.replace(hour=9))   # sent once
    [(when, msg)] = poster.sent
    assert msg.kind == "canvas_move" and msg.mention is False and msg.header.startswith("Canvas moved 2 dates")
    assert len(msg.undo) == 2 and len(msg.body) == 2
    q3 = service.get_by_ref(Q3)
    eid = [e for e, tid in msg.undo if tid == q3.id][0]
    assert db.get_event(service.conn, eid)["kind"] == "rescheduled"
    service.undo(eid, actor="button")                          # what the Undo button does
    assert service.get(q3.id).due_at == datetime(2026, 10, 16, 23, 59, tzinfo=ET)
    # the next morning post also names the remaining (not undone) move
    from dispatch import scheduler
    moved = scheduler.moved_by_canvas(service, night.replace(hour=8))
    assert [tid for tid, _ in moved] != [] and q3.id not in [tid for tid, _ in moved]


def test_canvas_adds_fill_a_free_slot_by_rank_not_file_order(service, clock):
    clock.set(NOW)
    service.add("active 1", area="School")
    service.add("active 2", area="School")                   # one slot free
    heavy = service.add("coreflow waiting", area="Coreflow/CLARA", payoff=5, status="queued")
    canvas.sync(service, NOW, lambda: TEXT, guess=lambda e: G)
    active = {t.id for t in service.by_status("active")}
    # the first VEVENT in the file (Quiz 3) would have taken the slot in arrival order;
    # by rank the Coreflow payoff-5 item beats every Canvas item (none due within 48 h)
    assert heavy.id in active and len(active) == 3
    assert all(t.status == "queued" for t in service.by_status("queued") if t.source == "canvas")



# ---- review r2 should-fixes: items created before feed-date tracking existed ---------------------
def _forget_tracking(service, legacy_created_detail=False):
    """Make the database look like one written before round 2: no canvas_feed_due:* rows and
    (optionally) created events without "feed_due" (round-1 rows stored only "due_at")."""
    service.conn.execute("DELETE FROM meta WHERE key LIKE 'canvas_feed_due:%'")
    if legacy_created_detail:
        service.conn.execute("UPDATE events SET detail = json_remove(detail, '$.feed_due') "
                             "WHERE kind='created' AND json_extract(detail, '$.via') = 'canvas'")
    service.conn.commit()


@pytest.mark.parametrize("legacy", [False, True], ids=["feed_due-in-event", "round-1-due_at-only"])
def test_fallback_for_items_created_before_tracking(service, clock, legacy):
    clock.set(NOW)
    canvas.sync(service, NOW, lambda: TEXT, guess=lambda e: G)
    _forget_tracking(service, legacy)
    q3 = service.get_by_ref(Q3)
    mine = datetime(2026, 10, 15, 18, 0, tzinfo=ET)
    service.reschedule(q3.id, mine, actor="chat")               # Max's own date
    for h in (2, 4):
        res = canvas.sync(service, NOW + timedelta(hours=h), lambda: TEXT)
        assert res.moved == []
    assert service.get(q3.id).due_at == mine
    res = canvas.sync(service, NOW + timedelta(hours=6), lambda: moved_text("20261019T035900Z"))
    assert [t.id for t in res.moved] == [q3.id]                   # a real feed move: applied once
    res = canvas.sync(service, NOW + timedelta(hours=8), lambda: moved_text("20261019T035900Z"))
    assert res.moved == []


def test_first_sight_without_any_record_uses_the_task_date_as_baseline(service, clock):
    """No meta row and no feed date in the created event (hand-made / imported item), and Max
    never moved it: a feed date different from the task's date is a real move."""
    clock.set(NOW)
    t = service.add("Quiz 3", area="School", source="canvas", external_ref=Q3,
                    due_at=datetime(2026, 10, 16, 23, 59, tzinfo=ET))
    service.conn.execute("UPDATE events SET detail = json_remove(detail, '$.due_at') WHERE task_id=?",
                         (t.id,))
    service.conn.commit()
    res = canvas.sync(service, NOW, lambda: moved_text("20261019T035900Z"), guess=lambda e: G)
    assert [x.id for x in res.moved] == [t.id]
    assert service.get(t.id).due_at == datetime(2026, 10, 18, 23, 59, tzinfo=ET)



def test_dry_run_shows_canvas_move_notes():
    """review r2 should-fix 4: the dry run exercises the same move-note path as the bot."""
    from dispatch.__main__ import SEED_PATH, dry_run
    lines = []
    dry_run("2026-10-05T07:00:00-04:00", 27, SEED_PATH, out=lambda s="": lines.append(str(s)),
            canvas_fixture=FIXTURE, canvas_fixture_later=FIXTURE.with_name("canvas-sample-moved.ics"))
    text = "\n".join(lines)
    assert "#today (canvas_move)" in text and "buttons: [Undo #19]" in text
    assert "now due Sun 10/18 11:59 PM (was Fri 10/16 11:59 PM)" in text
    assert "during quiet hours 00:00-07:59 ET: 0" in text
