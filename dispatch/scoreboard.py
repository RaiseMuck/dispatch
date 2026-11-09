"""Live scoreboard: one message in #scoreboard that the bot keeps editing (added 2026-10-10).

Max asked for counts, not item lists: daily-three picks done, streak, time spent, the share of
the week's expected value he captured, and EV per hour to pair the two. No titles appear.

  EV of an item     = payoff (1-5) x likelihood it matters (0-1): the same two numbers the
                      ranking uses (rules._parts, same defaults 3 and 70 % when missing).
  Time of an item   = its estimate in minutes (30 when missing). "Set by you" when the
                      estimate exists and 'estimate' is not in the item's guessed list.
  EV captured %     = EV done this week / (EV done + EV missed). Missed = items that were on
                      this week's plate and not done within it: in a ranked morning three this
                      week, or due by the end of the week (overdue included). Killed items
                      (dropped on purpose) and items now waiting/parked are not misses.
  EV per hour       = EV done / hours done (estimates), same week.
Weeks are US weeks (Sunday start), US Eastern, matching the rest of Dispatch.
"""
from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta

from . import db, rules, stats
from .clock import ET

META_MESSAGE = "scoreboard_message_id"
BLOCKS = "▁▂▃▄▅▆▇█"
COLOR = 0x5865F2
TREND_DAYS = 14
TREND_WEEKS = 6


def week_start(d: date) -> date:
    """The Sunday on or before d."""
    return d - timedelta(days=(d.weekday() + 1) % 7)


def item_ev(t, now: datetime) -> float:
    p = rules._parts(t, now)
    return p["payoff"] * p["likelihood"]


def item_minutes(t) -> int:
    return t.estimate_minutes if t.estimate_minutes and t.estimate_minutes > 0 \
        else rules.DEFAULT_ESTIMATE_MIN


def estimate_set_by_max(t) -> bool:
    return bool(t.estimate_minutes) and "estimate" not in (t.guessed or [])


def _dt(d: date) -> datetime:
    return datetime.combine(d, time(0), tzinfo=ET)


def _done_ids(conn, lo: str, hi: str) -> set[int]:
    return {r[0] for r in conn.execute(
        "SELECT DISTINCT task_id FROM events WHERE kind = 'done' AND undone_by IS NULL "
        "AND task_id IS NOT NULL AND at >= ? AND at < ?", (lo, hi))}


def _posted_ids(conn, lo: str, hi: str) -> set[int]:
    ids = set()
    for tid, detail in conn.execute(
            "SELECT task_id, detail FROM events WHERE kind = 'morning_post' AND at >= ? AND at < ?",
            (lo, hi)):
        try:
            rank = json.loads(detail).get("rank") if detail else None
        except ValueError:
            rank = None
        if tid and isinstance(rank, int) and 1 <= rank <= 3:
            ids.add(tid)
    return ids


def _tasks(conn, ids) -> list:
    ids = sorted(ids)
    if not ids:
        return []
    return db.tasks_where(conn, f"id IN ({','.join('?' * len(ids))})", tuple(ids))


def week(conn, start: date, now: datetime, until: date | None = None) -> dict:
    """Numbers for the US week starting `start`. `until` (today) stops the picks count early."""
    end = start + timedelta(days=7)
    lo_dt, hi_dt = _dt(start), _dt(end)
    lo, hi = stats._utc(start), stats._utc(end)
    done_ids = _done_ids(conn, lo, hi)
    done = [t for t in _tasks(conn, done_ids) if t.status == "done"]
    ev_done = sum(item_ev(t, now) for t in done)
    minutes = sum(item_minutes(t) for t in done)

    plate = _posted_ids(conn, lo, hi)
    plate |= {r[0] for r in conn.execute(
        "SELECT id FROM tasks WHERE due_at IS NOT NULL AND due_at < ? AND created_at < ?",
        (hi, hi))}
    missed = []
    for t in _tasks(conn, plate - done_ids):
        if t.closed_at is not None and t.closed_at < lo_dt:
            continue                                   # finished before this week
        if t.status == "killed" and t.closed_at is not None and t.closed_at < hi_dt:
            continue                                   # dropped on purpose, not a miss
        if t.status in ("waiting", "parked"):
            continue
        missed.append(t)
    ev_missed = sum(item_ev(t, now) for t in missed)

    hits = posted = 0
    last = min(end - timedelta(days=1), until) if until else end - timedelta(days=1)
    d = start
    while d <= last:
        s = stats.day_stats(conn, d)
        if s["status"] == "ok":
            hits += s["hits"]
            posted += s["posted"]
        d += timedelta(days=1)

    total = ev_done + ev_missed
    return {"start": start.isoformat(), "done": len(done), "minutes": minutes,
            "minutes_set_by_max": sum(1 for t in done if estimate_set_by_max(t)),
            "ev_done": round(ev_done, 2), "ev_missed": round(ev_missed, 2),
            "ev_pct": round(ev_done / total, 3) if total else None,
            "ev_per_hour": round(ev_done / (minutes / 60), 2) if minutes else None,
            "picks_hits": hits, "picks_posted": posted}


def compute(conn, now: datetime) -> dict:
    today = now.astimezone(ET).date()
    ws = week_start(today)
    weeks = [week(conn, ws - timedelta(days=7 * i), now, until=today)
             for i in range(TREND_WEEKS - 1, -1, -1)]
    trend = []
    for i in range(TREND_DAYS - 1, -1, -1):
        s = stats.day_stats(conn, today - timedelta(days=i))
        trend.append({"date": s["date"], "posted": s["posted"], "hits": s["hits"],
                      "ok": s["status"] == "ok"})
    td = trend[-1]
    all_done = [t for t in _tasks(conn, _done_ids(conn, "0", "9")) if t.status == "done"]
    return {"today": td, "this_week": weeks[-1], "last_week": weeks[-2], "weeks": weeks,
            "trend": trend, "streak": stats.streak(conn, today),
            "minutes_all_time": sum(item_minutes(t) for t in all_done)}


# ---- rendering: a Discord embed as a plain dict (discord.Embed.from_dict in the bot) ----------
def fmt_minutes(m: int) -> str:
    h, mm = divmod(int(m), 60)
    return f"{h}h {mm:02d}m" if h else f"{mm}m"


def _pct(x) -> str:
    return "—" if x is None else f"{x:.0%}"


def _bar_char(hits: int, posted: int) -> str:
    if not posted:
        return "·"
    r = hits / posted
    return BLOCKS[0] if r == 0 else BLOCKS[max(1, round(r * 7))]


def _bar(x, width: int = 10) -> str:
    n = 0 if x is None else round(x * width)
    return "█" * n + "░" * (width - n)


def embed_dict(data: dict, stamp: str | None = None) -> dict:
    tw, lw, td = data["this_week"], data["last_week"], data["today"]
    today = f"{td['hits']} / {td['posted']} done" if td["ok"] else "no ranked post yet"
    picks = (f"{tw['picks_hits']} / {tw['picks_posted']} ({tw['picks_hits'] / tw['picks_posted']:.0%})"
             if tw["picks_posted"] else "no data yet")
    days = data["streak"]
    eph = "—" if tw["ev_per_hour"] is None else f"{tw['ev_per_hour']:.1f}"
    eph_last = "—" if lw["ev_per_hour"] is None else f"{lw['ev_per_hour']:.1f}"
    letters = " ".join(date.fromisoformat(d["date"]).strftime("%a")[0] for d in data["trend"])
    bars = " ".join(_bar_char(d["hits"], d["posted"]) if d["ok"] else "·" for d in data["trend"])
    wk = "\n".join(
        f"{date.fromisoformat(w['start']).strftime('%b %d')}  {_bar(w['ev_pct'])} {_pct(w['ev_pct']):>4}"
        for w in data["weeks"])
    footer = (f"EV = payoff × chance it matters. Time = estimates "
              f"({tw['minutes_set_by_max']} of {tw['done']} this week set by you).")
    if stamp:
        footer += f" Updated {stamp}"
    return {
        "title": "Dispatch scoreboard",
        "color": COLOR,
        "fields": [
            {"name": "Today's three", "value": today, "inline": True},
            {"name": "Picks this week", "value": picks, "inline": True},
            {"name": "Streak", "value": f"{days} day{'s' if days != 1 else ''}", "inline": True},
            {"name": "Time this week", "value": f"{fmt_minutes(tw['minutes'])}\n"
             f"*all-time {fmt_minutes(data['minutes_all_time'])}*", "inline": True},
            {"name": "EV captured", "value": f"{_pct(tw['ev_pct'])}\n*{tw['ev_done']:.1f} of "
             f"{tw['ev_done'] + tw['ev_missed']:.1f} · last wk {_pct(lw['ev_pct'])}*", "inline": True},
            {"name": "EV per hour", "value": f"{eph}\n*last wk {eph_last}*", "inline": True},
            {"name": f"Picks done, last {len(data['trend'])} days",
             "value": f"```\n{letters}\n{bars}\n```", "inline": False},
            {"name": "EV captured by week", "value": f"```\n{wk}\n```", "inline": False},
        ],
        "footer": {"text": footer},
    }
