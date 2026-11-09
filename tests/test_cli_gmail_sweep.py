"""The exact command the Gmail sweep runs, as a real `python -m dispatch cli` process:
    python -m dispatch cli add --json --source gmail --external-ref gmail:<id> --due "<date>" "<title>"
A second run for the same message adds nothing."""
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from dispatch import db
from dispatch.clock import ET

ROOT = Path(__file__).resolve().parents[1]


def sweep_add(dbfile, ref, due, title):
    env = dict(os.environ, OLLAMA_URL="http://127.0.0.1:9")   # no AI: refused at once, standard numbers
    p = subprocess.run([sys.executable, "-m", "dispatch", "cli", "add", "--json", "--source", "gmail",
                        "--external-ref", ref, "--due", due, title, "--db", str(dbfile)],
                       cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert p.stdout.count("\n") == 1, (p.stdout, p.stderr)       # exactly one JSON object
    return p.returncode, json.loads(p.stdout)


def test_gmail_sweep_add_end_to_end_and_dedupe(tmp_path):
    dbfile = tmp_path / "sweep.db"
    conn = db.connect(dbfile)
    db.migrate(conn, datetime(2026, 10, 5, 9, 0, tzinfo=ET))
    conn.close()

    code, first = sweep_add(dbfile, "gmail:19a7f3c2", "2030-10-16T17:00", "Reply to Sam about the pilot")
    assert code == 0 and first["ok"] is True and first["created"] is True
    t = first["task"]
    assert (t["source"], t["external_ref"], t["title"]) == ("gmail", "gmail:19a7f3c2",
                                                             "Reply to Sam about the pilot")
    assert t["due_at"] == "2030-10-16T17:00:00-04:00"

    code, again = sweep_add(dbfile, "gmail:19a7f3c2", "2030-10-17T09:00", "Reply to Sam (re-sweep)")
    assert code == 0 and again["created"] is False and again["task"]["id"] == t["id"]
    assert again["task"]["due_at"] == t["due_at"]                   # the existing task, untouched
    conn = db.connect(dbfile)
    try:
        assert conn.execute("SELECT COUNT(*) FROM tasks WHERE external_ref='gmail:19a7f3c2'").fetchone()[0] == 1
    finally:
        conn.close()
