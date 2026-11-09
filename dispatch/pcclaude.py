"""Claude Code on this PC, run headless (added 2026-10-18), used read-only for Max's calendar:
every 3 hours (07:00-23:00 ET) Claude reads his Google Calendar through his own connected
account and Dispatch caches the next 8 days for the phone line's week view. The only tool it
is allowed is the Google Calendar connector. No Google keys on disk.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from .clock import ET

log = logging.getLogger("dispatch.pcclaude")

CAL_KEY = "calendar_cache"
CAL_REFRESH = timedelta(hours=3)
CAL_HOURS = (7, 23)
CAL_TIMEOUT = 180
HOME = Path.home()
CAL_TOOLS = "mcp__claude_ai_Google_Calendar__list_events,mcp__claude_ai_Google_Calendar__list_calendars"

CAL_PROMPT = ("Use the Google Calendar tools to list every event on all my calendars from {start} "
              "through {end} (America/New_York). Only read; never create, change or delete "
              "anything. Reply with ONLY a JSON array, no prose: "
              '[{{"start": ISO datetime with offset or YYYY-MM-DD, "end": same, "title": string, '
              '"all_day": bool, "location": string or null}}]')


def claude_path() -> str | None:
    p = HOME / ".local" / "bin" / "claude.exe"
    return shutil.which("claude") or (str(p) if p.exists() else None)


async def run_claude(prompt: str, tools: str, model: str | None = None, timeout: int = 300,
                     max_turns: int = 12) -> tuple[bool, str]:
    """(ok, result text). Never raises."""
    exe = claude_path()
    if not exe:
        return False, "Claude Code is not installed on this PC."
    args = [exe, "-p", prompt, "--output-format", "json", "--allowedTools", tools,
            "--max-turns", str(max_turns)]
    if model:
        args += ["--model", model]
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=str(HOME), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        if proc is not None:
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        return False, f"timed out after {timeout} s"
    except Exception as exc:  # noqa: BLE001
        return False, f"could not start ({type(exc).__name__})"
    text = out.decode("utf-8", errors="replace")
    try:
        obj = json.loads(text)
    except ValueError:
        return False, (text or err.decode("utf-8", errors="replace"))[-500:]
    return (not obj.get("is_error")), str(obj.get("result") or "")


def parse_events(text: str) -> list[dict] | None:
    m = re.search(r"\[.*\]", text or "", re.DOTALL)
    if not m:
        return None
    try:
        arr = json.loads(m.group(0))
    except ValueError:
        return None
    out = []
    for e in arr if isinstance(arr, list) else []:
        if isinstance(e, dict) and isinstance(e.get("title"), str) and e.get("start"):
            out.append({"start": str(e["start"]), "end": str(e.get("end") or ""),
                        "title": e["title"][:120], "all_day": bool(e.get("all_day")),
                        "location": (e.get("location") or None)})
    return out


def calendar(service) -> dict:
    v = service.meta_get(CAL_KEY)
    return json.loads(v) if v else {}


def calendar_stale(service, now: datetime) -> bool:
    c = calendar(service)
    if not c.get("fetched_at"):
        return True
    h = now.astimezone(ET).hour
    return (CAL_HOURS[0] <= h < CAL_HOURS[1]
            and now - datetime.fromisoformat(c["fetched_at"]) >= CAL_REFRESH)


async def refresh_calendar(service, now: datetime, runner=run_claude) -> bool:
    end = (now + timedelta(days=8)).astimezone(ET).date().isoformat()
    ok, text = await runner(CAL_PROMPT.format(start=now.astimezone(ET).date().isoformat(), end=end),
                            CAL_TOOLS, model="haiku", timeout=CAL_TIMEOUT)
    events = parse_events(text) if ok else None
    c = calendar(service)
    if events is None:
        log.info("calendar refresh failed: %s", (text or "")[:120])
        # keep the old events; try again in 30 minutes
        c["fetched_at"] = (now - CAL_REFRESH + timedelta(minutes=30)).isoformat()
        c["failed_at"] = now.isoformat()
        service.meta_set(CAL_KEY, json.dumps(c))
        return False
    service.meta_set(CAL_KEY, json.dumps({"fetched_at": now.isoformat(), "events": events}))
    return True


def _start_of(e: dict) -> datetime | None:
    s = e["start"]
    try:
        if len(s) == 10:
            return datetime.fromisoformat(s).replace(tzinfo=ET)
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(ET)
    except ValueError:
        return None


def events_on(service, day) -> list[dict]:
    out = []
    for e in calendar(service).get("events", []):
        st = _start_of(e)
        if st is not None and st.date() == day:
            out.append({**e, "_start": st})
    return sorted(out, key=lambda e: (not e["all_day"], e["_start"]))
