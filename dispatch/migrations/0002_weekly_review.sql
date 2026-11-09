-- 0002: allow the 'weekly_review' event kind (Sunday list of queued + parked items).
-- SQLite cannot edit a CHECK rule in place, so the events table is rebuilt and its rows copied.
CREATE TABLE events_new (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id  INTEGER REFERENCES tasks(id),
    kind     TEXT NOT NULL CHECK (kind IN ('created','snoozed','rescheduled','done','killed',
                                           'nag_sent','promoted','do_now','morning_post',
                                           'weekly_review')),
    at       TEXT NOT NULL,
    detail   TEXT
);
INSERT INTO events_new (id, task_id, kind, at, detail) SELECT id, task_id, kind, at, detail FROM events;
DROP TABLE events;
ALTER TABLE events_new RENAME TO events;
CREATE INDEX idx_events_task ON events(task_id);
