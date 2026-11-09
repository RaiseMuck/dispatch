-- 0004: Phase 2.1 ("fits my day").
--   tasks:  new status 'waiting' (blocked on someone else, with a check-in date) and five
--           columns: place (home/out/phone), energy (light/focused), swap_count (swaps over
--           the item's lifetime), waiting_until (check-in moment), parent_id (split-off step).
--   events: new kinds swapped, waiting, resumed, mode_set, repick, split, needs_set,
--           checkin_sent; an index on the message tag so exactly-once lookups are cheap.
-- Same rebuild pattern as 0003: db.migrate() turns foreign keys OFF around this script and
-- runs PRAGMA foreign_key_check before recording the version. BEGIN/COMMIT = all-or-nothing.
BEGIN;

CREATE TEMP TABLE _seq_before AS SELECT name, seq FROM sqlite_sequence;

CREATE TABLE tasks_new (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    title            TEXT    NOT NULL,
    area             TEXT    NOT NULL DEFAULT 'unsorted',
    status           TEXT    NOT NULL CHECK (status IN ('active','queued','parked','waiting','done','killed')),
    due_at           TEXT,
    estimate_minutes INTEGER,
    payoff           REAL,
    likelihood       REAL,
    snooze_count     INTEGER NOT NULL DEFAULT 0,
    next_nag_at      TEXT,
    created_at       TEXT    NOT NULL,
    closed_at        TEXT,
    source           TEXT    NOT NULL CHECK (source IN ('inbox','seed','claude','canvas','vault','gmail')),
    notes            TEXT,
    external_ref     TEXT,
    guessed          TEXT,
    place            TEXT    CHECK (place IN ('home','out','phone')),
    energy           TEXT    CHECK (energy IN ('light','focused')),
    swap_count       INTEGER NOT NULL DEFAULT 0,
    waiting_until    TEXT,
    parent_id        INTEGER REFERENCES tasks(id)
);
INSERT INTO tasks_new (id, title, area, status, due_at, estimate_minutes, payoff, likelihood,
                       snooze_count, next_nag_at, created_at, closed_at, source, notes,
                       external_ref, guessed)
    SELECT id, title, area, status, due_at, estimate_minutes, payoff, likelihood,
           snooze_count, next_nag_at, created_at, closed_at, source, notes,
           external_ref, guessed FROM tasks;
DROP TABLE tasks;
ALTER TABLE tasks_new RENAME TO tasks;
CREATE INDEX idx_tasks_status ON tasks(status);
CREATE UNIQUE INDEX idx_tasks_external_ref ON tasks(external_ref) WHERE external_ref IS NOT NULL;
CREATE INDEX idx_tasks_next_nag ON tasks(next_nag_at);
CREATE INDEX idx_tasks_parent ON tasks(parent_id);
CREATE INDEX idx_tasks_waiting ON tasks(waiting_until);

CREATE TABLE events_new (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id   INTEGER REFERENCES tasks(id),
    kind      TEXT NOT NULL CHECK (kind IN ('created','snoozed','rescheduled','done','killed',
                                            'nag_sent','promoted','do_now','morning_post',
                                            'weekly_review','estimated','prioritized','area_set',
                                            'shifted','undone','chat',
                                            'swapped','waiting','resumed','mode_set','repick',
                                            'split','needs_set','checkin_sent')),
    at        TEXT NOT NULL,
    detail    TEXT,
    actor     TEXT,
    cause_id  INTEGER REFERENCES events(id),
    undone_by INTEGER REFERENCES events(id)
);
INSERT INTO events_new (id, task_id, kind, at, detail, actor, cause_id, undone_by)
    SELECT id, task_id, kind, at, detail, actor, cause_id, undone_by FROM events;
DROP TABLE events;
ALTER TABLE events_new RENAME TO events;
CREATE INDEX idx_events_task ON events(task_id);
CREATE INDEX idx_events_kind_at ON events(kind, at);
CREATE INDEX idx_events_cause ON events(cause_id);
CREATE INDEX idx_events_msg ON events(json_extract(detail, '$.msg'));

-- Keep the AUTOINCREMENT counters at least where they were (ids are never reused).
UPDATE sqlite_sequence
   SET seq = MAX(seq, COALESCE((SELECT b.seq FROM _seq_before b WHERE b.name = sqlite_sequence.name), 0))
 WHERE name IN ('tasks', 'events');
DROP TABLE _seq_before;

COMMIT;
