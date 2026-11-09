"""Migrate a COPY of a database and print before/after facts (row counts, identity, checks).

    python tools/migration_check.py tests/fixtures/live-copy-2026-09-28.db

The file given is never modified: it is copied with the SQLite backup API into a temp folder
first. Used for the Phase 2 report; the same checks run in tests/test_migration.py.
"""
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dispatch import db  # noqa: E402
from dispatch.clock import ET  # noqa: E402

TARGET_VERSION = max(int(f.name.split("_", 1)[0]) for f in db.MIGRATIONS_DIR.glob("*.sql"))
COLS = "id, title, area, status, due_at, estimate_minutes, payoff, likelihood, snooze_count, " \
       "next_nag_at, created_at, closed_at, source, notes"


def facts(path: Path) -> dict:
    c = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    out = {"version": c.execute("SELECT MAX(version) FROM schema_version").fetchone()[0],
           "tasks": c.execute(f"SELECT {COLS} FROM tasks ORDER BY id").fetchall(),
           "events": c.execute("SELECT id, task_id, kind, at, detail FROM events ORDER BY id").fetchall(),
           "meta": c.execute("SELECT key, value FROM meta ORDER BY key").fetchall(),
           "seq": dict(c.execute("SELECT name, seq FROM sqlite_sequence").fetchall()),
           "status": dict(c.execute("SELECT status, COUNT(*) FROM tasks GROUP BY status").fetchall())}
    c.close()
    return out


def main(src: str) -> int:
    work = Path(tempfile.mkdtemp(prefix="dispatch-migcheck-")) / "copy.db"
    s = sqlite3.connect(f"file:{Path(src).resolve().as_posix()}?mode=ro", uri=True)
    d = sqlite3.connect(work)
    s.backup(d)
    d.close()
    s.close()
    before = facts(work)
    conn = db.connect(work)
    applied = db.migrate(conn, datetime.now(ET))
    checks = (conn.execute("PRAGMA integrity_check").fetchone()[0],
              conn.execute("PRAGMA foreign_key_check").fetchall(),
              conn.execute("PRAGMA journal_mode").fetchone()[0])
    conn.close()
    after = facts(work)
    print(f"source (read-only): {src}")
    print(f"schema version: {before['version']} -> {after['version']} (applied {applied})")
    print(f"tasks: {len(before['tasks'])} -> {len(after['tasks'])}; by status before {before['status']} after {after['status']}")
    print(f"events: {len(before['events'])} -> {len(after['events'])}; meta rows: {len(before['meta'])} -> {len(after['meta'])}")
    print(f"every task row identical (14 Phase 1 columns): {before['tasks'] == after['tasks']}")
    print(f"every event row identical: {before['events'] == after['events']}; meta identical: {before['meta'] == after['meta']}")
    print(f"id counters (sqlite_sequence): {before['seq']} -> {after['seq']}")
    print(f"integrity_check: {checks[0]}; foreign_key_check: {checks[1] or 'no problems'}; journal_mode: {checks[2]}")
    ok = (after["version"] == TARGET_VERSION and before["tasks"] == after["tasks"] and before["events"] == after["events"]
          and checks[0] == "ok" and not checks[1])
    print("RESULT:", "PASS - no data lost" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
