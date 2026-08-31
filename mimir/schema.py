"""SQLite schema for one Mimir instance.

One instance = one database file = one consumer's fully isolated memory.
Never shared across consumers — see SPEC.md §2, §8.
"""

import json
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

-- One accumulating record per (topic, layer), not one row per colliding
-- pair — found during hardening: the original node_a/node_b-pair design
-- created a new row for every combination, O(n^2) in the number of
-- colliding writes (measured: 6 same-layer writes to one topic -> 15
-- rows). node_ids holds every node still competing for this collision, as
-- a JSON array. The partial unique index below makes "at most one OPEN
-- conflict per (topic, layer)" a database-enforced invariant, not just
-- application logic that could drift.
CREATE TABLE IF NOT EXISTS conflicts (
    id           TEXT PRIMARY KEY,
    topic        TEXT NOT NULL,
    layer        TEXT NOT NULL,
    node_ids     TEXT NOT NULL,   -- JSON array, all nodes competing for this (topic, layer)
    detected_at  TEXT NOT NULL,
    resolved_at  TEXT,
    resolution   TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_conflicts_open_topic_layer
    ON conflicts(topic, layer) WHERE resolved_at IS NULL;

-- Branch/leaf recall relevance filtering runs through this, not a Python-side
-- substring scan of every row — a scale test (scripts/scale_test.py) showed
-- the naive approach is O(n) in node count (100 nodes: 0.7ms: 20,000 nodes:
-- 130ms, linear) — it didn't fix recall lag, it relocated it. FTS5 pushes
-- the search into SQLite's own index instead. External-content table: the
-- real data stays in `nodes`, this only ever holds the search index.
CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
    topic, description, content, tags,
    content='nodes', content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS nodes_fts_ai AFTER INSERT ON nodes BEGIN
    INSERT INTO nodes_fts(rowid, topic, description, content, tags)
    VALUES (new.rowid, new.topic, new.description, new.content, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS nodes_fts_ad AFTER DELETE ON nodes BEGIN
    INSERT INTO nodes_fts(nodes_fts, rowid, topic, description, content, tags)
    VALUES ('delete', old.rowid, old.topic, old.description, old.content, old.tags);
END;

CREATE TRIGGER IF NOT EXISTS nodes_fts_au AFTER UPDATE ON nodes BEGIN
    INSERT INTO nodes_fts(nodes_fts, rowid, topic, description, content, tags)
    VALUES ('delete', old.rowid, old.topic, old.description, old.content, old.tags);
    INSERT INTO nodes_fts(rowid, topic, description, content, tags)
    VALUES (new.rowid, new.topic, new.description, new.content, new.tags);
END;
"""


def _migrate_conflicts_table_if_needed(conn: sqlite3.Connection) -> None:
    """One-time migration from the original node_a/node_b-pair conflicts
    schema (O(n^2) rows per collision) to one accumulating record per
    (topic, layer). Runs before CREATE TABLE IF NOT EXISTS, since that
    would otherwise leave an old-shaped table untouched forever."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(conflicts)")}
    if not cols or "node_a" not in cols:
        return  # no conflicts table yet, or already migrated

    old_rows = conn.execute("SELECT * FROM conflicts").fetchall()
    conn.execute("ALTER TABLE conflicts RENAME TO conflicts_old_migrated")

    groups: dict[tuple[str, str], dict] = {}
    for row in old_rows:
        node = conn.execute(
            "SELECT layer FROM nodes WHERE id IN (?, ?) LIMIT 1", (row["node_a"], row["node_b"])
        ).fetchone()
        layer = node["layer"] if node else "unknown"  # both nodes gone — best-effort
        key = (row["topic"], layer)
        g = groups.setdefault(key, {
            "id": row["id"], "node_ids": set(), "detected_at": row["detected_at"],
            "resolved_at": row["resolved_at"], "resolution": row["resolution"],
        })
        g["node_ids"].update([row["node_a"], row["node_b"]])
        g["detected_at"] = min(g["detected_at"], row["detected_at"])

    conn.executescript("""
        CREATE TABLE conflicts (
            id TEXT PRIMARY KEY, topic TEXT NOT NULL, layer TEXT NOT NULL,
            node_ids TEXT NOT NULL, detected_at TEXT NOT NULL,
            resolved_at TEXT, resolution TEXT
        );
        CREATE UNIQUE INDEX idx_conflicts_open_topic_layer
            ON conflicts(topic, layer) WHERE resolved_at IS NULL;
    """)
    for (topic, layer), g in groups.items():
        conn.execute(
            "INSERT INTO conflicts (id, topic, layer, node_ids, detected_at, resolved_at, resolution) "
            "VALUES (?,?,?,?,?,?,?)",
            (g["id"], topic, layer, json.dumps(sorted(g["node_ids"])), g["detected_at"],
             g["resolved_at"], g["resolution"]),
        )
    conn.execute("DROP TABLE conflicts_old_migrated")
    conn.commit()


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open (and if needed, initialize) one instance's database file.

    Each instance is a single file — see SPEC.md §4. Never point two
    different consumers at the same path.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    # Must check for nodes_fts's existence BEFORE running the schema script,
    # and must check sqlite_master, not row counts: count(*) on an
    # external-content FTS5 table reads through to its backing table
    # (`nodes`) regardless of whether the inverted index was ever actually
    # built, so it's always non-zero once `nodes` has rows — comparing it
    # to node_count can never detect a stale/never-indexed table. This was
    # a real bug: it meant `rebuild` never ran, and recall() silently
    # returned zero branch/leaf matches for a database's entire existing
    # history, on every node, discovered only by testing recall against
    # real content and finding known-good matches missing.
    fts_existed_before = conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='nodes_fts'"
    ).fetchone()[0] > 0

    _migrate_conflicts_table_if_needed(conn)

    conn.executescript(SCHEMA)

    if not fts_existed_before:
        # First time this database gets an FTS5 index — either it's brand
        # new (no-op, nothing to index yet) or it predates this schema
        # version (existing `nodes` rows the triggers never saw). `rebuild`
        # is FTS5's documented way to build an external-content index from
        # its backing table from scratch.
        conn.execute("INSERT INTO nodes_fts(nodes_fts) VALUES ('rebuild')")

    conn.commit()
    return conn
