"""The Phase 2 success test: "you finish at least half of the daily three".

    python -m dispatch stats --days 7 [--json] [--db PATH]

For each US-Eastern day, the "three" are the items the morning post ranked 1-3 (its
morning_post events carry "rank"). A hit = that item was marked done between the post and 8 AM
the next day (and the done was not undone). Days with no ranked post (bot off, or before the
Phase 2 switch - Phase 1 posts have no rank) are "no data", never 0 %.
Pass = at least half of the three done on at least 4 of 7 days.

Phase 2.1 adds, per day: every item marked done (done_total, incl. "did:" off-list work), the
day mode, swaps and items set to waiting; and a streak. A day is GOOD when at least half of
the three were done, or when it was a sick/slammed day and at least one thing got done; a day
with no ranked post and no mode is neutral (PC off); anything else ends the streak. Today only
counts once it is good (it is still in progress).
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

from . import db
from .clock import ET, SystemClock
from .config import load_settings

NEXT_MORNING = time(8, 0)


def _utc(d: date, t: time = time(0)) -> str:
    return db.to_db(datetime.combine(d, t, tzinfo=ET))


def _day_extras(conn: sqlite3.Connection, d: date) -> dict:
    lo, hi = _utc(d), _utc(d + timedelta(days=1))
    done_rows = conn.execute(
        "SELECT e.task_id, c.detail AS created FROM events e LEFT JOIN events c ON "
        "c.task_id = e.task_id AND c.kind = 'created' WHERE e.kind = 'done' AND "
        "e.undone_by IS NULL AND e.at >= ? AND e.at < ?", (lo, hi)).fetchall()
    did = sum(1 for r in done_rows if r["created"] and json.loads(r["created"]).get("did"))
    raw = db.meta_get(conn, "day_mode:" + d.isoformat())
    try:
        mode = json.loads(raw).get("mode") if raw else None
    except ValueError:
        mode = None

    def count(kind: str) -> int:
        return conn.execute("SELECT COUNT(*) FROM events WHERE kind = ? AND undone_by IS NULL "
                            "AND at >= ? AND at < ?", (kind, lo, hi)).fetchone()[0]
    return {"done_total": len(done_rows), "did": did, "mode": mode, "swaps": count("swapped"),
            "waiting_set": count("waiting")}


def day_good(d: dict) -> bool | None:
    """True = good day, False = ends the streak, None = neutral (no post, no mode)."""
    if d["status"] == "ok" and d["hits"] * 2 >= d["posted"]:
        return True
    if d.get("mode") in ("sick", "slammed"):
        return d.get("done_total", 0) >= 1
    if d["status"] != "ok":
        return None
    return False


def streak(conn: sqlite3.Connection, today: date, limit: int = 60) -> int:
    n = 0
    for i in range(limit):
        d = day_stats(conn, today - timedelta(days=i))
        good = day_good(d)
        if good is None or (i == 0 and good is False):
            continue                   # neutral day, or today not finished yet
        if not good:
            break
        n += 1
    return n


def day_stats(conn: sqlite3.Connection, d: date) -> dict:
    rows = conn.execute(
        "SELECT e.task_id, e.at, e.detail, t.title FROM events e LEFT JOIN tasks t ON t.id = e.task_id "
        "WHERE e.kind = 'morning_post' AND e.at >= ? AND e.at < ? ORDER BY e.id",
        (_utc(d), _utc(d + timedelta(days=1)))).fetchall()
    three: dict[int, dict] = {}
    post_at = None
    for r in rows:
        det = json.loads(r["detail"]) if r["detail"] else {}
        rank = det.get("rank")
        if isinstance(rank, int) and 1 <= rank <= 3 and r["task_id"] not in three:
            three[r["task_id"]] = {"id": r["task_id"], "title": r["title"], "rank": rank}
            post_at = r["at"] if post_at is None or r["at"] < post_at else post_at
    out = {"date": d.isoformat(), "weekday": d.strftime("%a"), "posted": len(three), "hits": 0,
           "rate": None, "items": sorted(three.values(), key=lambda x: x["rank"]),
           **_day_extras(conn, d)}
    if not three:
        out["status"] = "no data"
        return out
    until = _utc(d + timedelta(days=1), NEXT_MORNING)
    for it in out["items"]:
        done = conn.execute("SELECT 1 FROM events WHERE task_id = ? AND kind = 'done' AND "
                            "undone_by IS NULL AND at >= ? AND at < ? LIMIT 1",
                            (it["id"], post_at, until)).fetchone()
        it["done"] = bool(done)
        out["hits"] += int(bool(done))
    out["rate"] = round(out["hits"] / out["posted"], 3)
    out["status"] = "ok"
    return out


def compute(conn: sqlite3.Connection, now: datetime, days: int = 7) -> dict:
    today = now.astimezone(ET).date()
    per_day = [day_stats(conn, today - timedelta(days=i)) for i in range(days - 1, -1, -1)]
    with_post = [d for d in per_day if d["status"] == "ok"]
    posted = sum(d["posted"] for d in with_post)
    hits = sum(d["hits"] for d in with_post)
    half = [d for d in with_post if d["hits"] * 2 >= d["posted"]]
    need = math.ceil(days * 4 / 7)                   # 4 of 7 days, scaled for other windows
    since = db.to_db(datetime.combine(today - timedelta(days=days - 1), time(0), tzinfo=ET))

    def count(sql: str, *params) -> int:
        return conn.execute(sql, (since, *params)).fetchone()[0]

    chat_rows = conn.execute("SELECT detail FROM events WHERE kind = 'chat' AND at >= ?", (since,)).fetchall()
    chat_actions = [json.loads(r[0]).get("action") if r[0] else None for r in chat_rows]
    by_source = dict(conn.execute(
        "SELECT t.source, COUNT(*) FROM events e JOIN tasks t ON t.id = e.task_id "
        "WHERE e.kind = 'created' AND e.at >= ? AND e.actor IN ('cli','canvas','siri','chat') "
        "GROUP BY t.source", (since,)).fetchall())
    extras = {
        "chat_messages": len(chat_rows) + count(
            "SELECT COUNT(*) FROM events WHERE at >= ? AND actor = 'chat' AND kind NOT IN "
            "('chat','undone','promoted')"),
        "chat_unclear": sum(1 for a in chat_actions if a == "unclear"),
        "chat_asked_which": sum(1 for a in chat_actions if a == "ambiguous"),
        "undos": count("SELECT COUNT(*) FROM events WHERE kind = 'undone' AND at >= ?"),
        "adds_by_actor": dict(conn.execute(
            "SELECT COALESCE(actor, 'unknown'), COUNT(*) FROM events WHERE kind = 'created' "
            "AND at >= ? GROUP BY actor", (since,)).fetchall()),
        "adds_by_source": by_source,
        "canvas_moved": count("SELECT COUNT(*) FROM events WHERE kind = 'rescheduled' AND "
                              "actor = 'canvas' AND at >= ?"),
        "swaps": count("SELECT COUNT(*) FROM events WHERE kind = 'swapped' AND undone_by IS NULL "
                       "AND at >= ?"),
        "waiting_set": count("SELECT COUNT(*) FROM events WHERE kind = 'waiting' AND "
                             "undone_by IS NULL AND at >= ?"),
        "modes_set": count("SELECT COUNT(*) FROM events WHERE kind = 'mode_set' AND "
                           "undone_by IS NULL AND at >= ?"),
        "splits": count("SELECT COUNT(*) FROM events WHERE kind = 'split' AND undone_by IS NULL "
                        "AND at >= ?"),
        "needs_guessed": count("SELECT COUNT(*) FROM events WHERE kind = 'needs_set' AND "
                               "actor = 'qwen' AND at >= ?"),
        "needs_corrected": count("SELECT COUNT(*) FROM events WHERE kind = 'needs_set' AND "
                                 "actor != 'qwen' AND at >= ?"),
    }
    return {"days": per_day, "days_with_post": len(with_post), "days_at_or_above_half": len(half),
            "posted_total": posted, "hits_total": hits,
            "hit_rate_overall": round(hits / posted, 3) if posted else None,
            "success_needed_days": need, "success": len(half) >= need, "extras": extras,
            "streak": streak(conn, today),
            "done_total": sum(d["done_total"] for d in per_day),
            "did_total": sum(d["did"] for d in per_day)}


def render(res: dict) -> str:
    lines = [f"Daily-three hit rate, last {len(res['days'])} days (US Eastern):"]
    for d in res["days"]:
        tail = f" - done today: {d.get('done_total', 0)}"
        if d.get("did"):
            tail += f" ({d['did']} off-list)"
        if d.get("mode") and d["mode"] != "normal":
            tail += f" - Mode: {d['mode']}"
        if d["status"] != "ok":
            lines.append(f"  {d['weekday']} {d['date']}: no data (no ranked morning post){tail}")
            continue
        marks = ", ".join(f"#{i['id']}{' done' if i['done'] else ''}" for i in d["items"])
        lines.append(f"  {d['weekday']} {d['date']}: {d['hits']}/{d['posted']} ({d['rate']:.0%}) - "
                     f"{marks}{tail}")
    rate = res["hit_rate_overall"]
    lines.append(f"Overall: {res['hits_total']}/{res['posted_total']}"
                 + (f" ({rate:.0%})" if rate is not None else "")
                 + f" on {res['days_with_post']} day(s) with a post; "
                 f"{res['days_at_or_above_half']} day(s) at or above half.")
    verdict = ("PASS" if res["success"] else "FAIL so far") if res["days_with_post"] else \
        "not measurable yet (no ranked morning post in this window)"
    lines.append(f"Success test (>= half of the daily three on >= {res['success_needed_days']} of "
                 f"{len(res['days'])} days): {verdict}")
    e = res["extras"]
    lines.append(f"Chat: {e['chat_messages']} message(s), {e['chat_unclear']} not understood, "
                 f"{e['chat_asked_which']} 'which one?' questions; undos: {e['undos']}; "
                 f"adds by: {e['adds_by_actor'] or 'none'}; Canvas date moves: {e['canvas_moved']}")
    lines.append(f"Streak: {res.get('streak', 0)} day(s) (a sick/slammed day with something done "
                 "keeps it going). Done in total: "
                 f"{res.get('done_total', 0)} ({res.get('did_total', 0)} logged with did:).")
    lines.append(f"Fits my day: {e.get('modes_set', 0)} day mode(s), {e.get('swaps', 0)} swap(s), "
                 f"{e.get('waiting_set', 0)} set to waiting, {e.get('splits', 0)} split(s); "
                 f"place/energy guessed {e.get('needs_guessed', 0)}, corrected "
                 f"{e.get('needs_corrected', 0)}.")
    return "\n".join(lines)


def main(argv: list[str] | None = None, clock=None, out=None) -> int:
    out = out or sys.stdout
    ap = argparse.ArgumentParser(prog="python -m dispatch stats")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--db", type=Path)
    a = ap.parse_args(argv)
    path = a.db or load_settings().db_path
    if not Path(path).exists():
        print(f"No database at {path}.", file=out)
        return 1
    conn = db.connect(path)
    try:
        res = compute(conn, (clock or SystemClock()).now(), max(a.days, 1))
    finally:
        conn.close()
    out.write((json.dumps(res, default=str) if a.json else render(res)) + "\n")
    return 0
