"""Phone errands, v1 (added 2026-10-18): "email me the Costco memo" while driving.

Flow: on a call the phone AI writes the request in one sentence, reads it back, and only after
Max says yes calls the computer_errand tool (the tools server checks it is Max's own number).
Dispatch queues it, posts it in #today, and runs one errand at a time through Claude Code on
this PC (pcclaude.run_claude) with a deliberately narrow tool set:
  - his files: READ ONLY (Read, Glob, Grep) - no shell, no writing, no deleting;
  - his Gmail and Google Drive connectors, with the rule that email goes only to him.
The result (or what Claude needs) is posted back in #today. Anything bigger than "find it and
send it to me" is out of scope for v1 on purpose; widen ERRAND_TOOLS only deliberately.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .pcclaude import run_claude

JOBS_KEY = "phone_errands"
JOB_TIMEOUT = 600
HOME = Path.home()
MAX_EMAIL = "owner@example.com"  # the only address errands may email; set to your own
ERRAND_TOOLS = ("Read,Glob,Grep,mcp__claude_ai_Gmail__*,mcp__claude_ai_Google_Drive__*")

RULES = f"""Max asked for this on a phone call to his Dispatch line and confirmed the exact wording out loud:

"{{instruction}}"

Do it now. You can only read and search his files (under {HOME}) and use his Gmail and Google Drive.
- Email goes ONLY to {MAX_EMAIL}. If the request asks to email anyone else, do not send; say so.
- To send a file, attach it if the Gmail tools allow, otherwise upload it to his Google Drive and email him the link.
- Never delete, trash or change existing emails, files or Drive items.
- If it's ambiguous (e.g. two files could match), don't guess: send nothing and list the candidates.
- Finish with at most 3 plain sentences: what you sent and where, or what you need from him."""


def errands(service) -> list[dict]:
    v = service.meta_get(JOBS_KEY)
    return json.loads(v) if v else []


def _save(service, js: list[dict]) -> None:
    service.meta_set(JOBS_KEY, json.dumps(js[-50:]))


def queue(service, instruction: str, now: datetime, call_id: str | None = None) -> dict:
    js = errands(service)
    job = {"id": max([j["id"] for j in js], default=0) + 1, "at": now.isoformat(),
           "instruction": " ".join(instruction.split())[:800], "status": "queued",
           "call": call_id, "result": None}
    js.append(job)
    _save(service, js)
    return job


def recover(service) -> list[dict]:
    """At start-up: an errand that was running when the bot stopped is marked interrupted."""
    js, hit = errands(service), []
    for j in js:
        if j["status"] == "running":
            j["status"], j["result"] = "interrupted", "The PC or bot restarted while it ran."
            hit.append(j)
    if hit:
        _save(service, js)
    return hit


async def run_next(service, runner=run_claude) -> dict | None:
    """Run the oldest queued errand if none is running. Returns it when finished."""
    js = errands(service)
    if any(j["status"] == "running" for j in js):
        return None
    nxt = next((j for j in js if j["status"] == "queued"), None)
    if nxt is None:
        return None
    nxt["status"] = "running"
    _save(service, js)
    ok, text = await runner(RULES.format(instruction=nxt["instruction"]), ERRAND_TOOLS,
                            timeout=JOB_TIMEOUT, max_turns=25)
    js = errands(service)
    for j in js:
        if j["id"] == nxt["id"]:
            j["status"], j["result"] = ("done" if ok else "failed"), (text or "")[:1500]
            nxt = j
    _save(service, js)
    return nxt


def render(job: dict) -> str:
    if job["status"] == "queued":
        return f"\U0001F5A5️ Errand #{job['id']} from your call: \"{job['instruction']}\" - working on it."
    head = {"done": "done", "failed": "didn't finish", "interrupted": "was interrupted"}[job["status"]]
    return f"\U0001F5A5️ Errand #{job['id']} {head}: {job['result'] or '(no details)'}"
