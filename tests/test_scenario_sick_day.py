"""The dry-run 'sick day' scenario (Phase 2.1 plan 10.3): Mon 10/5, Sick/home day at 8:05."""
from datetime import datetime
from pathlib import Path

from dispatch import rules
from dispatch.__main__ import dry_run
from dispatch.clock import ET

SEED = Path(__file__).resolve().parent / "fixtures" / "seed-sick-day.json"


def test_sick_day_scenario():
    lines, got = [], {}
    rc = dry_run("2026-10-05T07:00:00-04:00", 27, SEED, out=lines.append, scenario="sick-day",
                 on_done=lambda s, script, poster: got.update(s=s, script=script, poster=poster))
    assert rc == 0
    s, script, poster = got["s"], got["script"], got["poster"]
    text = "\n".join(lines)
    # after 08:05 the three are home items <= 45 min (the ids come from fit_rank)
    assert len(script.mode_three) == 3
    for tid in script.mode_three:
        assert rules.fits(s.get(tid), rules.SICK)
    assert "Max taps [Sick/home day]" in text and "Set aside for today:" in text
    # the overdue Strimer return (out) is in every hourly message of the sick day, flagged
    strimer = next(t for t in s.by_status("active", "queued") if "Strimer" in t.title)
    mode_at = datetime(2026, 10, 5, 8, 5, tzinfo=ET)
    nags = [(w, m) for w, m in poster.sent if m.kind == "nag" and mode_at <= w
            and w.date() == mode_at.date()]
    assert nags and all(strimer.id in m.flags for _, m in nags)
    assert all(m.flags[strimer.id].startswith("can't fit today (needs going out)")
               for _, m in nags)
    # never in quiet hours
    assert not [w for w, _ in poster.sent if rules.in_quiet_hours(w)]
    # the events: one mode, one executed swap, the did: (created + done)
    kinds = dict(s.conn.execute("SELECT kind, COUNT(*) FROM events GROUP BY kind").fetchall())
    assert kinds["mode_set"] == 1 and kinds["swapped"] == 1 and kinds["done"] >= 1
    assert s.conn.execute("SELECT COUNT(*) FROM tasks WHERE title='set up Dispatch' AND "
                          "status='done'").fetchone()[0] == 1
    assert "Nothing in the queue shorter than" in text             # the 10:00 "Too long"
    assert "Best fit for 20 min" in text and "[Start] [Skip]" in text
    # the next morning re-picks and says so
    assert "Back after yesterday's sick/home day:" in text
    assert kinds["repick"] == 1
