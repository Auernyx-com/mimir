"""SQLite schema for one Mimir instance.

One instance = one database file = one consumer's fully isolated memory.
Never shared across consumers — see SPEC.md §2, §8.
"""

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id              TEXT PRIMARY KEY,      -- UUID, see SPEC.md §4
    layer           TEXT NOT NULL CHECK (layer IN ('root','trunk','branch','leaf')),
    topic           TEXT NOT NULL,         -- collision key for conflict detection
    content         TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    tags            TEXT NOT NULL DEFAULT '[]',   -- JSON array
    created_at      TEXT NOT NULL,
    last_confirmed_at TEXT NOT NULL,
    expires_at      TEXT,                  -- leaf-only
    confirmations   INTEGER NOT NULL DEFAULT 0,
    authored_by     TEXT NOT NULL,
    origin_machine  TEXT NOT NULL,
    hash            TEXT NOT NULL,
    prev_hash       TEXT,                  -- per-node chain, NOT a global sequence
    superseded_by   TEXT REFERENCES nodes(id)
);

CREATE INDEX IF NOT EXISTS idx_nodes_topic ON nodes(topic);
CREATE INDEX IF NOT EXISTS idx_nodes_layer ON nodes(layer);
CREATE INDEX IF NOT EXISTS idx_nodes_expires ON nodes(expires_at);

CREATE TABLE IF NOT EXISTS links (
    from_id TEXT NOT NULL REFERENCES nodes(id),
    to_id   TEXT NOT NULL REFERENCES nodes(id),
    PRIMARY KEY (from_id, to_id)
);

CREATE TABLE IF NOT EXISTS edit_log (
    id       TEXT PRIMARY KEY,
    node_id  TEXT NOT NULL,   -- deliberately not a FK: edit_log must outlive
                               -- a pruned node (see storage.consolidate) —
                               -- it's the durable record of what existed.
    at       TEXT NOT NULL,
    actor    TEXT NOT NULL,
    diff     TEXT NOT NULL,   -- JSON
    hash     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_editlog_node ON edit_log(node_id);

CREATE TABLE IF NOT EXISTS conflicts (
    id           TEXT PRIMARY KEY,
    topic        TEXT NOT NULL,
    node_a       TEXT NOT NULL REFERENCES nodes(id),
    node_b       TEXT NOT NULL REFERENCES nodes(id),
    detected_at  TEXT NOT NULL,
    resolved_at  TEXT,
    resolution   TEXT
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open (and if needed, initialize) one instance's database file.

    Each instance is a single file — see SPEC.md §4. Never point two
    different consumers at the same path.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn
