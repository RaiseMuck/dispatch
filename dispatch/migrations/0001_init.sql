-- 0001: first schema. Times are stored as ISO-8601 text in UTC (e.g. 2026-10-03T15:00:00+00:00).
CREATE TABLE tasks (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    title            TEXT    NOT NULL,
    area             TEXT    NOT NULL DEFAULT 'unsorted',
    status           TEXT    NOT NULL CHECK (status IN ('active','queued','parked','done','killed')),
    due_at           TEXT,
    estimate_minutes INTEGER,
    payoff           REAL,           -- Phase 2 (Qwen ranking); unused now
    likelihood       REAL,           -- Phase 2; unused now
    snooze_count     INTEGER NOT NULL DEFAULT 0,
    next_nag_at      TEXT,
    created_at       TEXT    NOT NULL,
    closed_at        TEXT,
    source           TEXT    NOT NULL CHECK (source IN ('inbox','seed','claude','canvas','vault')),
    notes            TEXT
);
CREATE INDEX idx_tasks_status ON tasks(status);

CREATE TABLE events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id  INTEGER REFERENCES tasks(id),
    kind     TEXT NOT NULL CHECK (kind IN ('created','snoozed','rescheduled','done','killed',
                                           'nag_sent','promoted','do_now','morning_post')),
    at       TEXT NOT NULL,
    detail   TEXT             -- small JSON blob
);
CREATE INDEX idx_events_task ON events(task_id);

-- Scheduler memory (last morning post date, last batch time) so a restart never double-posts.
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
