"""SQLite storage: schema and connection helper.

Tables:
  series            - identity of a recurring series
  series_versions   - immutable rule versions; a version applies to local
                      dates >= its effective_from (later versions win)
  exceptions        - single-day overrides (cancel / move)
  occurrences       - CONFIRMED instances only: immutable evidence of the
                      rule version and resolution that applied
  reminders         - one unique row per (series, local_date)
  generator_runs    - audit log of every generator tick
"""
from __future__ import annotations

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS series (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS series_versions (
    series_id INTEGER NOT NULL REFERENCES series(id),
    version INTEGER NOT NULL,
    effective_from TEXT NOT NULL,   -- local date YYYY-MM-DD, inclusive
    tz TEXT NOT NULL,               -- IANA key
    freq TEXT NOT NULL,             -- daily | weekly
    interval INTEGER NOT NULL,
    byweekday TEXT NOT NULL,        -- JSON array of ints, Monday=0
    time_of_day TEXT NOT NULL,      -- HH:MM local wall time
    start_date TEXT NOT NULL,       -- local date
    until TEXT,                     -- local date or NULL
    on_gap TEXT NOT NULL,           -- skip | shift
    on_ambiguous TEXT NOT NULL,     -- first | second
    created_at TEXT NOT NULL,
    PRIMARY KEY (series_id, version)
);

CREATE TABLE IF NOT EXISTS exceptions (
    id INTEGER PRIMARY KEY,
    series_id INTEGER NOT NULL REFERENCES series(id),
    ex_date TEXT NOT NULL,          -- local date YYYY-MM-DD
    action TEXT NOT NULL,           -- cancel | move
    override_time TEXT,             -- HH:MM when action=move
    created_at TEXT NOT NULL,
    UNIQUE (series_id, ex_date)
);

-- Confirmed instances: immutable evidence.  Rows are written when an
-- instance is explicitly confirmed via the API or when its reminder is
-- generated, and are never rewritten afterwards.
CREATE TABLE IF NOT EXISTS occurrences (
    id INTEGER PRIMARY KEY,
    series_id INTEGER NOT NULL REFERENCES series(id),
    local_date TEXT NOT NULL,
    version INTEGER NOT NULL,       -- rule version used
    tz TEXT NOT NULL,
    intended_local TEXT NOT NULL,   -- rule's local time (pre-exception)
    effective_local TEXT NOT NULL,  -- after exception override
    actual_local TEXT,              -- after DST-gap shift; NULL if not applicable
    utc_start TEXT,                 -- resolved UTC instant; NULL when skipped/cancelled
    status TEXT NOT NULL,           -- scheduled | skipped | cancelled
    kind TEXT NOT NULL,             -- normal | ambiguous | gap-shifted | gap-skipped | cancelled
    skip_reason TEXT,
    confirmed_at TEXT NOT NULL,
    UNIQUE (series_id, local_date)
);

CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY,
    series_id INTEGER NOT NULL REFERENCES series(id),
    local_date TEXT NOT NULL,
    occurrence_id INTEGER NOT NULL REFERENCES occurrences(id),
    version INTEGER NOT NULL,       -- rule version used
    due_utc TEXT NOT NULL,          -- event_utc - lead time
    event_utc TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (series_id, local_date)
);

CREATE TABLE IF NOT EXISTS generator_runs (
    id INTEGER PRIMARY KEY,
    run_at TEXT NOT NULL,
    horizon TEXT NOT NULL,
    inserted INTEGER NOT NULL
);
"""


def connect(path: str) -> sqlite3.Connection:
    # check_same_thread=False: the Service serializes all access with a lock,
    # and the HTTP server handles requests on its own thread.
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn
