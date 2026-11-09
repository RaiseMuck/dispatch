"""Live eval of the chat parser against the REAL local Qwen (Ollama). Not a unit test.

    python tools/eval_qwen.py [--models qwen3.5:27b,qwen3.5:4b] [--out tools/eval-results]
                              [--set main|heldout|heldout21]

Every message runs through the real chat.Router (same prompt, validation, task resolution and
TaskService calls as the bot) on a fresh copy of a temp database loaded from seed.json, with
the clock pinned to Mon 2026-10-05 09:00 ET. Field guesses are switched off (defaults) so only
the chat parse is measured. Scoring looks at what the bot actually DID (events written),
not only at the action name the model said.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import statistics
import sys
import tempfile
import time
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dispatch import chat, db, qwen, rules  # noqa: E402
from dispatch.__main__ import SEED_PATH, import_seed  # noqa: E402
from dispatch.clock import ET, FakeClock  # noqa: E402
from dispatch.service import TaskService  # noqa: E402

URL = "http://127.0.0.1:11434"
NOW = datetime(2026, 10, 5, 9, 0, tzinfo=ET)
ASK = "ask"          # expected: the bot must ask (Which one?), never act


def D(month, day, h=9, m=0):
    return datetime(2026, month, day, h, m, tzinfo=ET)


# (message, expected effective action, task id | ASK | None, datetime | None, value | None)
CASES = [
    ("done #3", "done", 3, None, None),
    ("finished the Micro Center return", "done", 1, None, None),
    ("called Sam, that's done", "done", 3, None, None),
    ("kill the electroculture thing", "kill", 18, None, None),
    ("drop 14, not gonna learn git this month", "kill", 14, None, None),
    ("move the Strimer return to Saturday 1pm", "reschedule", 1, D(10, 10, 13), None),
    ("push the Keystone sessions to next Wednesday", "reschedule", 2, D(10, 14), None),  # Max's rule
    ("insurance broker call - Friday", "reschedule", 4, D(10, 9), None),
    ("snooze #11 until 3pm", "snooze", 11, D(10, 5, 15), None),
    ("later tonight for the lease thing", "snooze", 8, D(10, 5, 20), None),
    ("not today, do the XYZ target tomorrow", "reschedule", 10, D(10, 6), None),
    ("that's more like 3 hours", "re_estimate", 11, None, 180),
    ("the MacBook backup is a 20 minute job", "re_estimate", 12, None, 20),
    ("low priority: THESIS_TRACKER renumbering", "set_priority", 9, None, 2),
    ("the broker call is critical", "set_priority", 4, None, 5),
    ("#6 is actually Personal admin", "set_area", 6, None, "Personal admin"),
    ("I'm slammed until 4, move everything after that", "shift_after", None, D(10, 5, 16), None),
    ("nothing until 2pm please", "shift_after", None, D(10, 5, 14), None),
    ("what's due today", "query:due_today", None, None, None),
    ("what's on my plate this week", "query:due_week", None, None, None),
    ("list", "query:active", None, None, None),
    ("what's parked", "query:parked", None, None, None),
    ("add renew the parking permit by Friday", "add", None, D(10, 9), "renew the parking permit"),
    ("remind me to email Prof about the memo tomorrow 10am", "add", None, D(10, 6, 10),
     "email Prof about the memo"),
    ("undo that", "undo", 3, None, None),
    ("do the 1099 thing tomorrow", "reschedule", ASK, None, None),
    ("done with the backup", "done", ASK, None, None),
    ("mark the Portfolio one done", "done", ASK, None, None),
    ("thanks", "unclear", None, None, None),
    ("I should probably call Sam at some point", "no_change", None, None, None),
    ("bike money: decide Friday, 2 hrs of work", "reschedule", 7, D(10, 9), None),
    ("asdf", "unclear", None, None, None),
    # Phase 2.1 (plan 11.3): day modes, minutes queries, place/energy corrections, waiting.
    # Seed ids in this database: #1 Strimer return, #3 Call Sam, #4 insurance broker,
    # #13 Mac Cowork duplicates.
    ("home sick today, keep it light", "set_mode", None, None, rules.SICK),
    ("sick, home only, maybe 2 hours in short bursts", "set_mode", None, None,
     rules.DayMode("sick", frozenset({"home"}), 30, True, 120)),
    ("slammed today, only quick stuff", "set_mode", None, None, rules.SLAMMED),
    ("back to normal", "set_mode", None, None, rules.NORMAL),          # after a sick-day mode
    ("I've got 30 minutes", "minutes", None, None, (30, False)),
    ("what can I do from bed", "minutes", None, None, (None, True)),
    ("the Strimer return is an out-of-the-house one", "set_needs", 1, None, ("place", "out")),
    ("easy one", "set_needs", 13, None, ("energy", "light")),         # chat_context = #13
    ("the Sam call is a phone thing", "set_needs", 3, None, ("place", "phone")),
    ("waiting on the broker to call back, #4", "wait", 4, D(10, 8), None),
]

# Held-out set, written in round 2 AFTER the last prompt change and never used to tune it
# (review r1: the round-1 few-shots mirrored two eval misses). Same seed ids and clock.
HELDOUT = [
    ("knocked out the MacBook backup", "done", 12, None, None),
    ("scrap the Mac Cowork duplicates task", "kill", 13, None, None),
    ("push the tuition lookup to Thursday 2pm", "reschedule", 15, D(10, 8, 14), None),
    ("the git loop will take about 45 minutes", "re_estimate", 14, None, 45),
    ("bike fund is not important", "set_priority", 7, None, 1),
    ("anything due this week?", "query:due_week", None, None, None),
    ("remind me to book a dentist appointment next Tuesday", "add", None, D(10, 13),
     "book a dentist appointment"),
    ("done with the 1099 stuff", "done", ASK, None, None),
    ("took me 2 tries but the Sam call is done", "done", 3, None, None),
    ("ok cool", "unclear", None, None, None),
]

# Phase 2.1 held-out set: written after the Phase 2.1 prompt was final, never used to tune it.
# (#11 PC workbench is made one of the three first, #3 Call Sam is set to waiting first, so
# the swap and the resume have something to act on - see PREP.)
HELDOUT21 = [
    ("feeling rough, staying in", "set_mode", None, None, rules.SICK),
    ("only got a quarter hour", "minutes", None, None, (15, False)),
    ("swap the workbench thing, way too long", "swap", 11, None, "long"),
    ("#6 is a focus job", "set_needs", 6, None, ("energy", "focused")),
    ("Sam finally answered", "resume", 3, None, None),
]


def _prep(s: TaskService, msg: str) -> None:
    """State a message needs before it is sent (the same for every model)."""
    if msg == "that's more like 3 hours":                 # plan 12.2 #12: chat_context = #11
        s.meta_set(chat.CONTEXT_KEY, json.dumps({"task_id": 11, "at": NOW.isoformat()}))
    elif msg == "easy one":                               # right after talking about #13
        s.meta_set(chat.CONTEXT_KEY, json.dumps({"task_id": 13, "at": NOW.isoformat()}))
    elif msg == "undo that":                              # something of Max's to undo
        s.snooze(3, rules.PLUS2H, actor="button")
    elif msg == "back to normal":                         # a sick day to come back from
        s.set_mode(rules.SICK, actor="button", source="button")
    elif msg == "swap the workbench thing, way too long":
        s.start(11, actor="button")
    elif msg == "Sam finally answered":
        s.set_waiting(3, actor="button")

EFFECT = {"done": "done", "killed": "kill", "snoozed": "snooze", "rescheduled": "reschedule",
          "estimated": "re_estimate", "prioritized": "set_priority", "area_set": "set_area",
          "do_now": "do_now", "shifted": "shift_after", "created": "add", "undone": "undo",
          "mode_set": "set_mode", "needs_set": "set_needs", "waiting": "wait",
          "resumed": "resume", "swapped": "swap"}


async def _inline(fn, *a):
    return fn(*a)


def build_base(path: Path) -> None:
    conn = db.connect(path)
    clock = FakeClock(D(10, 4))
    db.migrate(conn, clock.now())
    import_seed(TaskService(conn, clock), SEED_PATH)
    conn.close()


def copy_db(src: Path, dst: Path) -> None:
    s, d = sqlite3.connect(src), sqlite3.connect(dst)
    s.backup(d)
    s.close()
    d.close()


def unload(model: str) -> None:
    body = json.dumps({"model": model, "keep_alive": 0}).encode("utf-8")
    req = urllib.request.Request(URL + "/api/generate", data=body,
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=60).read()


def pctl(xs: list[float], p: float) -> float:
    """Nearest-rank percentile (p95 of 32 samples = the 31st smallest)."""
    import math
    xs = sorted(xs)
    return xs[max(0, math.ceil(p * len(xs)) - 1)]


def run_case(base: Path, work: Path, model: str, i: int, case) -> dict:
    msg, exp_action, exp_task, exp_dt, exp_val = case
    path = work / f"{model.replace(':', '_')}-{i:02d}.db"
    copy_db(base, path)
    clock = FakeClock(NOW)
    conn = db.connect(path)
    s = TaskService(conn, clock)
    _prep(s, msg)
    start = conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]
    timing: dict = {}

    def ask(prompt):
        t0 = time.perf_counter()
        try:
            obj = qwen.generate(model, prompt, URL, timeout=180)
        finally:
            timing["ms"] = int((time.perf_counter() - t0) * 1000)
        timing["raw"] = obj
        return obj

    router = chat.Router(s, ask=ask, guess=lambda t, n: qwen.Guess(), run=_inline)
    reply = asyncio.run(router.handle(msg))
    evs = conn.execute("SELECT * FROM events WHERE id > ? AND kind NOT IN ('chat','promoted') "
                       "ORDER BY id", (start,)).fetchall()
    effect = EFFECT.get(evs[0]["kind"], evs[0]["kind"]) if evs else reply.action
    acted: set[int] = set()
    for e in evs:
        if e["kind"] == "shifted":
            continue
        if e["kind"] == "undone":
            acted.add(db.get_event(conn, db.detail_of(e)["event_id"])["task_id"])
        elif e["task_id"] is not None:
            acted.add(e["task_id"])
    got_dt = got_val = None
    ok = wrong = False
    if exp_task == ASK:
        ok = not evs and reply.action in ("ambiguous", "ask_which")
        wrong = bool(evs)
    elif exp_action == "no_change":
        ok = not evs and (reply.action in ("unclear", "add_unconfirmed") or reply.action.startswith("query"))
        wrong = bool(evs)
    elif exp_action == "unclear" or exp_action.startswith("query:"):
        ok = not evs and reply.action == exp_action
        wrong = bool(evs)
    elif exp_action == "minutes":                         # a suggestion: no row may change
        q = s.meta_json(chat.MINUTES_KEY) or {}
        got_val = (q.get("n"), q.get("bed"))
        ok = not evs and reply.action == "minutes" and got_val == exp_val
        wrong = bool(evs)
    elif exp_action == "set_mode":
        rec = s.mode_record() or {}
        got_val = rules.DayMode.from_json(rec.get("constraints") or {}) if rec else None
        if rec.get("mode") == "normal":
            got_val = rules.NORMAL
        ok = effect == "set_mode" and got_val == exp_val
        wrong = False                                     # a day mode names no task
    else:
        ok = effect == exp_action
        if exp_task is not None:
            ok = ok and acted == {exp_task}
            wrong = bool(acted) and acted != {exp_task}
        elif exp_action in ("shift_after",):
            wrong = False
        t = s.get(next(iter(acted))) if acted else None
        if exp_action == "reschedule" and t:
            got_dt = t.due_at
        elif exp_action == "snooze" and t:
            got_dt = t.next_nag_at
        elif exp_action == "shift_after" and evs:
            got_dt = datetime.fromisoformat(db.detail_of(evs[0])["until"]).astimezone(ET)
        elif exp_action == "add" and t:
            got_dt, got_val = t.due_at, t.title
        elif exp_action == "re_estimate" and t:
            got_val = t.estimate_minutes
        elif exp_action == "set_priority" and t:
            got_val = int(t.payoff) if t.payoff is not None else None
        elif exp_action == "set_area" and t:
            got_val = t.area
        elif exp_action == "set_needs" and t:
            got_val = (exp_val[0], getattr(t, exp_val[0]))
        elif exp_action == "wait" and t:
            got_dt = t.waiting_until
        elif exp_action == "swap" and evs:
            got_val = db.detail_of(evs[0]).get("reason")
        if exp_dt is not None:
            ok = ok and got_dt == exp_dt
        if exp_val is not None:
            ok = ok and (str(got_val).casefold() == str(exp_val).casefold())
    if isinstance(got_val, rules.DayMode):
        got_val = got_val.describe() + f" [{got_val.name}]"
    conn.close()
    got = effect
    if acted:
        got += " #" + ",#".join(str(x) for x in sorted(acted))
    if got_dt is not None:
        got += f" {got_dt.strftime('%a %m/%d %H:%M')}"
    if got_val is not None:
        got += f" = {got_val}"
    return {"i": i + 1, "message": msg, "expected": _exp_str(case), "got": got, "ok": ok,
            "wrong_task": wrong, "ms": timing.get("ms"), "raw": timing.get("raw"),
            "reply": reply.text[:160], "group": "ask" if exp_task == ASK else exp_action.split(":")[0]}


def _exp_str(case) -> str:
    msg, a, t, dt, v = case
    if isinstance(v, rules.DayMode):
        v = v.describe() + f" [{v.name}]"
    s = a if t != ASK else f"ASK ({a}?)"
    if isinstance(t, int):
        s += f" #{t}"
    if dt is not None:
        s += f" {dt.strftime('%a %m/%d %H:%M')}"
    if v is not None:
        s += f" = {v}"
    return s


def cold_load_ms(model: str, all_models: list[str], prompt: str) -> int:
    for m in all_models:
        try:
            unload(m)
        except Exception:  # noqa: BLE001
            pass
    time.sleep(2)
    t0 = time.perf_counter()
    qwen.generate(model, prompt, URL, timeout=300)
    return int((time.perf_counter() - t0) * 1000)


def guess_latency(model: str, base: Path) -> dict:
    """Time the add-time field guess (area, payoff, likelihood, estimate) on every seed title."""
    conn = db.connect(base)
    titles = [(r[0], r[1], r[2]) for r in conn.execute("SELECT title, notes, area FROM tasks ORDER BY id")]
    conn.close()
    ms, valid, area_ok = [], 0, 0
    for title, notes, area in titles:
        t0 = time.perf_counter()
        obj = qwen.generate(model, qwen.build_guess_prompt(title, notes), URL, timeout=180,
                            num_predict=160)
        ms.append(int((time.perf_counter() - t0) * 1000))
        g = qwen.validate_guess(obj)
        valid += int(len(g.guessed) == len(qwen.GUESS_FIELDS))
        area_ok += int(g.area == area)
    return {"n": len(titles), "all_six_fields": valid, "area_matches_seed": area_ok,
            "median_ms": int(statistics.median(ms)), "p95_ms": pctl(ms, 0.95), "max_ms": max(ms)}


def summarize(model: str, rows: list[dict], cold: int) -> dict:
    ms = [r["ms"] for r in rows if r["ms"] is not None]
    groups: dict[str, list[int]] = {}
    for r in rows:
        g = groups.setdefault(r["group"], [0, 0])
        g[0] += int(r["ok"])
        g[1] += 1
    return {"model": model, "correct": sum(r["ok"] for r in rows), "total": len(rows),
            "wrong_task": sum(r["wrong_task"] for r in rows),
            "median_ms": int(statistics.median(ms)) if ms else None,
            "p95_ms": pctl(ms, 0.95) if ms else None, "max_ms": max(ms) if ms else None,
            "cold_load_ms": cold, "by_group": groups,
            "over_10s": sum(1 for x in ms if x > 10_000), "over_6s": sum(1 for x in ms if x > 6_000)}


def markdown(results: dict, summaries: list[dict]) -> str:
    models = [s["model"] for s in summaries]
    out = ["# Live Qwen chat eval", "",
           f"Run {datetime.now(ET):%Y-%m-%d %H:%M} ET against Ollama {URL}; clock pinned to "
           f"{NOW:%a %Y-%m-%d %H:%M} ET; seed.json tasks; temperature 0; format json; think off.", "",
           "## Summary", "",
           "| model | correct | wrong-task executions | median ms (warm) | p95 ms (warm) | max ms | cold load ms |",
           "|---|---|---|---|---|---|---|"]
    for s in summaries:
        out.append(f"| {s['model']} | {s['correct']}/{s['total']} | {s['wrong_task']} | {s['median_ms']} "
                   f"| {s['p95_ms']} | {s['max_ms']} | {s['cold_load_ms']} |")
    out += ["", "## Field guesses at add time (all 18 seed titles)", "",
            "| model | all six fields valid | area = the area Max gave in seed.json | median ms | p95 ms | max ms |",
            "|---|---|---|---|---|---|"]
    for s in summaries:
        f = s.get("field_guess") or {}
        out.append(f"| {s['model']} | {f.get('all_six_fields')}/{f.get('n')} | {f.get('area_matches_seed')}/"
                   f"{f.get('n')} | {f.get('median_ms')} | {f.get('p95_ms')} | {f.get('max_ms')} |")
    out += ["", "## Per action type (correct/total)", "",
            "| expected action | " + " | ".join(models) + " |", "|---|" + "---|" * len(models)]
    groups = sorted({g for s in summaries for g in s["by_group"]})
    for g in groups:
        cells = [f"{s['by_group'].get(g, [0, 0])[0]}/{s['by_group'].get(g, [0, 0])[1]}" for s in summaries]
        out.append(f"| {g} | " + " | ".join(cells) + " |")
    out += ["", "## Every message", "",
            "| # | message | expected | " + " | ".join(f"{m} result | ms" for m in models) + " |",
            "|---|---|---|" + "---|---|" * len(models)]
    for i in range(len(CASES)):
        row = [results[m][i] for m in models]
        cells = []
        for r in row:
            mark = "OK" if r["ok"] else ("**WRONG TASK**" if r["wrong_task"] else "miss")
            cells.append(f"{mark}: {r['got']} | {r['ms']}")
        out.append(f"| {i + 1} | {row[0]['message']} | {row[0]['expected']} | " + " | ".join(cells) + " |")
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="qwen3.5:27b,qwen3.5:4b")
    ap.add_argument("--out", type=Path, default=ROOT / "tools" / "eval-results")
    ap.add_argument("--set", choices=["main", "heldout", "heldout21"], default="main",
                    help="main = the 32 Phase 2 + 10 Phase 2.1 plan messages; heldout = 10 "
                         "round-2 sentences; heldout21 = 5 Phase 2.1 sentences (never tuned on)")
    a = ap.parse_args()
    global CASES
    if a.set == "heldout":
        CASES = HELDOUT
    elif a.set == "heldout21":
        CASES = HELDOUT21
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    models = [m.strip() for m in a.models.split(",") if m.strip()]
    work = Path(tempfile.mkdtemp(prefix="dispatch-eval-"))
    base = work / "base.db"
    build_base(base)
    conn = db.connect(base)
    warm_prompt = chat.build_prompt("hello", NOW, TaskService(conn, FakeClock(NOW)).open_tasks())
    conn.close()
    results, summaries = {}, []
    for model in models:
        cold = cold_load_ms(model, models, warm_prompt)
        print(f"{model}: cold load + first answer {cold} ms", flush=True)
        rows = []
        for i, case in enumerate(CASES):
            r = run_case(base, work, model, i, case)
            rows.append(r)
            flag = "OK  " if r["ok"] else ("WRONG" if r["wrong_task"] else "miss")
            print(f"  [{flag}] {r['i']:>2} {r['ms']:>6} ms  {r['message'][:48]:<48} -> {r['got']}", flush=True)
        results[model] = rows
        summ = summarize(model, rows, cold)
        summ["field_guess"] = guess_latency(model, base)
        print(f"  field guesses: {summ['field_guess']}", flush=True)
        summaries.append(summ)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    Path(str(a.out) + ".json").write_text(json.dumps({"summaries": summaries, "results": results},
                                                     indent=1, default=str), encoding="utf-8")
    md = markdown(results, summaries)
    Path(str(a.out) + ".md").write_text(md, encoding="utf-8")
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
