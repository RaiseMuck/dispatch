"""Migration 0003 (Phase 2): no data loss, references intact, new rules in force.

Runs on a synthetic Phase 1 (v2) database built from seed.json, and - when present - on a
read-only copy of the live database (tests/fixtures/live-copy-2026-09-28.db, gitignored).
"""
import json
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from dispatch import db
from dispatch.clock import ET

ROOT = Path(__file__).resolve().parent.parent
LIVE_COPY = Path(__file__).resolve().parent / "fixtures" / "live-copy-2026-09-28.db"
NOW = datetime(2026, 10, 5, 7, 0, tzinfo=ET)


def build_v2(path: Path) -> None:
    """A database exactly as Phase 1 leaves it: 0001 + 0002, seed tasks, a mix of events."""
    conn = sqlite3.connect(str(path))
    for name in ("0001_init.sql", "0002_weekly_review.sql"):
        conn.executescript((db.MIGRATIONS_DIR / name).read_text(encoding="utf-8"))
    conn.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
    conn.executemany("INSERT INTO schema_version VALUES (?, ?)",
                     [(1, "2026-09-28T20:00:00+00:00"), (2, "2026-09-28T20:00:00+00:00")])
    items = json.loads((ROOT / "seed.json").read_text(encoding="utf-8"))["items"]
    for i, it in enumerate(items, 1):
        status = it.get("status") or ("active" if i <= 3 else "queued")
        due = (datetime.fromisoformat(it["due_at"]).astimezone(ET) if it.get("due_at") else None)
        conn.execute("INSERT INTO tasks(title, area, status, due_at, snooze_count, next_nag_at, "
                     "created_at, source, notes) VALUES (?,?,?,?,?,?,?,?,?)",
                     (it["title"], it["area"], status, db.to_db(due), 1 if i == 2 else 0,
                      db.to_db(due), "2026-09-28T20:00:00+00:00", "seed", it.get("notes")))
        conn.execute("INSERT INTO events(task_id, kind, at, detail) VALUES (?,?,?,?)",
                     (i, "created", "2026-09-28T20:00:00+00:00", json.dumps({"status": status})))
    conn.execute("INSERT INTO events(task_id, kind, at, detail) VALUES (2,'snoozed','2026-09-29T13:00:00+00:00','{}')")
    conn.execute("INSERT INTO events(task_id, kind, at, detail) VALUES (4,'weekly_review','2026-10-04T12:00:00+00:00',NULL)")
    conn.execute("INSERT INTO events(task_id, kind, at, detail) VALUES (1,'morning_post','2026-09-29T12:00:00+00:00',NULL)")
    conn.execute("UPDATE tasks SET status='done', closed_at='2026-09-30T15:00:00+00:00' WHERE id=3")
    conn.execute("INSERT INTO events(task_id, kind, at, detail) VALUES (3,'done','2026-09-30T15:00:00+00:00',NULL)")
    conn.execute("INSERT INTO meta VALUES ('last_morning_date', '2026-10-04')")
    conn.commit()
    conn.close()


def snapshot(path: Path) -> dict:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    out = {
        "tasks": [tuple(r) for r in conn.execute(
            "SELECT id, title, area, status, due_at, estimate_minutes, payoff, likelihood, "
            "snooze_count, next_nag_at, created_at, closed_at, source, notes FROM tasks ORDER BY id")],
        "events": [tuple(r) for r in conn.execute(
            "SELECT id, task_id, kind, at, detail FROM events ORDER BY id")],
        "meta": [tuple(r) for r in conn.execute("SELECT key, value FROM meta ORDER BY key")],
        "seq": dict(conn.execute("SELECT name, seq FROM sqlite_sequence").fetchall()),
    }
    conn.close()
    return out


def check_migrated(path: Path, before: dict, expect=(3, 4)) -> sqlite3.Connection:
    conn = db.connect(path)
    assert db.migrate(conn, NOW) == list(expect)
    assert db.current_version(conn) == 4
    after = snapshot(path)
    assert after["tasks"] == before["tasks"]               # every column of every task identical
    assert after["events"] == before["events"]             # every event identical
    assert after["meta"] == before["meta"]
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    max_task = conn.execute("SELECT MAX(id) FROM tasks").fetchone()[0] or 0
    assert after["seq"].get("tasks", 0) >= max_task
    assert after["seq"].get("tasks", 0) >= before["seq"].get("tasks", 0)
    assert after["seq"].get("events", 0) >= before["seq"].get("events", 0)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(tasks)")]
    assert cols[-7:] == ["external_ref", "guessed", "place", "energy", "swap_count",
                         "waiting_until", "parent_id"]
    ecols = [r[1] for r in conn.execute("PRAGMA table_info(events)")]
    assert ecols[-3:] == ["actor", "cause_id", "undone_by"]
    idx = {r[1]: r[2] for r in conn.execute("PRAGMA index_list('tasks')")}
    assert idx.get("idx_tasks_external_ref") == 1          # unique
    assert {"idx_tasks_parent", "idx_tasks_waiting"} <= set(idx)
    eidx = {r[1] for r in conn.execute("PRAGMA index_list('events')")}
    assert "idx_events_msg" in eidx
    assert db.migrate(conn, NOW) == []                      # second run is a no-op
    return conn


def test_synthetic_v2_database_migrates_without_loss(tmp_path):
    path = tmp_path / "v2.db"
    build_v2(path)
    before = snapshot(path)
    assert len(before["tasks"]) == 18 and len(before["events"]) == 22
    conn = check_migrated(path, before)
    # the new rules are in force
    ins = ("INSERT INTO tasks(title, status, created_at, source, external_ref) "
           "VALUES (?, 'queued', '2026-10-05T11:00:00+00:00', ?, ?)")
    conn.execute(ins, ("from gmail", "gmail", "gmail:abc"))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(ins, ("same mail again", "gmail", "gmail:abc"))
    for i in range(3):                                      # NULL refs are unlimited
        conn.execute(ins, (f"no ref {i}", "claude", None))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(ins, ("bad source", "email", None))
    conn.execute("INSERT INTO events(task_id, kind, at, actor) VALUES (1, 'undone', 'x', 'chat')")
    with pytest.raises(sqlite3.IntegrityError):             # foreign keys are back ON
        conn.execute("INSERT INTO events(task_id, kind, at) VALUES (999, 'chat', 'x')")
    # Phase 2.1 rules
    conn.execute("UPDATE tasks SET status='waiting', waiting_until='2026-10-08T13:00:00+00:00' WHERE id=4")
    conn.execute("UPDATE tasks SET place='out', energy='focused', parent_id=1 WHERE id=5")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE tasks SET place='couch' WHERE id=5")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE tasks SET energy='tired' WHERE id=5")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE tasks SET parent_id=999 WHERE id=5")
    for kind in ("swapped", "waiting", "resumed", "mode_set", "repick", "split",
                 "needs_set", "checkin_sent"):
        conn.execute("INSERT INTO events(task_id, kind, at) VALUES (NULL, ?, 'x')", (kind,))
    t = db.get_task(conn, 5)
    assert (t.place, t.energy, t.parent_id, t.swap_count) == ("out", "focused", 1, 0)
    assert db.get_task(conn, 4).waiting_until is not None


def test_v3_database_takes_only_0004(tmp_path):
    """A database left at version 3 (e.g. by an earlier Phase 2 switch) gets 0004 alone."""
    path = tmp_path / "v3.db"
    build_v2(path)
    conn = sqlite3.connect(str(path))
    conn.executescript((db.MIGRATIONS_DIR / "0003_phase2.sql").read_text(encoding="utf-8"))
    conn.execute("INSERT INTO schema_version VALUES (3, '2026-10-05T11:00:00+00:00')")
    conn.execute("INSERT INTO events(task_id, kind, at, detail, actor, cause_id) "
                 "VALUES (1, 'estimated', '2026-10-05T12:00:00+00:00', ?, 'chat', 1)",
                 (json.dumps({"msg": 77}),))
    conn.execute("UPDATE tasks SET external_ref='canvas:x', guessed=? WHERE id=6",
                 (json.dumps(["area"]),))
    conn.commit()
    conn.close()
    before = snapshot(path)
    conn = check_migrated(path, before, expect=(4,))
    row = conn.execute("SELECT actor, cause_id FROM events WHERE kind='estimated'").fetchone()
    assert tuple(row) == ("chat", 1)                        # Phase 2 columns carried over
    assert [r["id"] for r in db.events_for_message(conn, 77)] == [before["events"][-1][0]]
    t = db.get_task(conn, 6)
    assert t.external_ref == "canvas:x" and t.guessed == ["area"]
    plan = " ".join(str(tuple(r)) for r in conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM events WHERE detail IS NOT NULL AND "
        "json_extract(detail, '$.msg') = 77"))
    assert "idx_events_msg" in plan


LIVE_COPIES = sorted(LIVE_COPY.parent.glob("live-copy-*.db"))   # 09-28 (Phase 2), 09-29 (2.1)


@pytest.mark.skipif(not LIVE_COPIES, reason="no read-only copy of the live DB on this machine")
@pytest.mark.parametrize("copy", LIVE_COPIES, ids=[p.stem for p in LIVE_COPIES])
def test_copy_of_the_live_database_migrates_without_loss(tmp_path, copy):
    path = tmp_path / "live-copy.db"
    shutil.copyfile(copy, path)                             # never touch the fixture itself
    before = snapshot(path)
    assert before["tasks"], "the live copy should contain tasks"
    conn = check_migrated(path, before)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_phase1_code_can_still_read_a_migrated_database(tmp_path):
    """Rollback safety: Phase 1 selects only its 14 columns - that still works after 0003/0004."""
    path = tmp_path / "v2.db"
    build_v2(path)
    conn = db.connect(path)
    db.migrate(conn, NOW)
    cols = ", ".join(db.V2_TASK_COLUMNS)
    rows = conn.execute(f"SELECT {cols} FROM tasks ORDER BY id").fetchall()
    tasks = [db.Task.from_row(r) for r in rows]
    assert len(tasks) == 18 and tasks[0].external_ref is None and tasks[0].guessed == []
    assert tasks[2].status == "done"



PHASE1_COMMIT = "cf88c19"          # the live Phase 1 code (rollback_phase2.ps1 returns to it)
PHASE1_SCRIPT = r'''
import sys, json
from datetime import datetime, timedelta
sys.path.insert(0, sys.argv[1])
from dispatch import db, __version__
from dispatch.clock import ET, FakeClock
from dispatch.service import TaskService
from dispatch.scheduler import PrintPoster, RunState, tick
assert __version__ == "0.1.0", __version__
clock = FakeClock(datetime(2026, 10, 8, 7, 0, tzinfo=ET))
conn = db.connect(sys.argv[2])
applied = db.migrate(conn, clock.now())                  # Phase 1 migrate on a v3 database
s = TaskService(conn, clock)
before = len(s.by_status("active", "queued", "parked", "done", "killed"))
t = s.add("added by phase 1 after rollback", area="School")
s.snooze(t.id, "plus2h")
q = s.by_status("queued")[0]
s.reschedule(q.id, clock.now() + timedelta(days=2))
a = s.by_status("active")[0]
s.mark_done(a.id)
poster = PrintPoster(clock, out=lambda *_: None)
for _ in range(90):                                      # 07:00 .. 08:29: the morning post
    tick(s, poster, clock, RunState())
    clock.advance(timedelta(minutes=1))
print(json.dumps({"applied": applied, "before": before,
                  "after": len(s.by_status("active", "queued", "parked", "done", "killed")),
                  "kinds": sorted({m.kind for _, m in poster.sent})}))
'''


def test_phase1_code_runs_on_the_upgraded_database(tmp_path):
    """rollback_phase2.ps1 KEEPS the current database by default. This runs the real Phase 1
    code (extracted from git at cf88c19) against a migrated v3 database: migrate is a no-op and
    add / snooze / reschedule / done / the morning post all work, with every row kept."""
    import subprocess
    import sys
    pkg = tmp_path / "phase1" / "dispatch"
    (pkg / "migrations").mkdir(parents=True)
    try:
        names = subprocess.run(["git", "-C", str(ROOT), "ls-tree", "--name-only", "-r",
                                PHASE1_COMMIT, "dispatch"], capture_output=True, text=True,
                               check=True).stdout.split()
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("git or the Phase 1 commit is not available here")
    for name in names:
        blob = subprocess.run(["git", "-C", str(ROOT), "show", f"{PHASE1_COMMIT}:{name}"],
                              capture_output=True, check=True).stdout
        (tmp_path / "phase1" / name).write_bytes(blob)
    path = tmp_path / "v4.db"
    build_v2(path)
    conn = db.connect(path)
    assert db.migrate(conn, NOW) == [3, 4]
    # a Phase 2.1 'waiting' item: invisible to Phase 1 (its by_status never selects it), kept
    conn.execute("UPDATE tasks SET status='waiting', waiting_until=next_nag_at WHERE id=18")
    conn.commit()
    conn.close()
    out = subprocess.run([sys.executable, "-c", PHASE1_SCRIPT, str(tmp_path / "phase1"), str(path)],
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    res = json.loads(out.stdout.strip().splitlines()[-1])
    assert res["applied"] == [] and res["after"] == res["before"] + 1
    assert "morning" in res["kinds"]
    conn = db.connect(path)
    assert db.current_version(conn) == 4
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    # and Phase 2 can take the database back afterwards (a re-switch after a rollback)
    assert db.migrate(conn, NOW) == []
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 19
    assert db.get_task(conn, 18).status == "waiting"
