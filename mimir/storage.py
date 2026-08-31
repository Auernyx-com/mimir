"""Core storage operations for one Mimir instance.

Every function here operates on exactly one instance's database file. There
is no code path anywhere in this module that reads or writes more than one
database — cross-instance sharing isn't a bug to guard against, it's not a
capability that exists. See SPEC.md §2, §8.
"""

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import GATED_LAYERS, LAYERS, DEFAULT_PROMOTION_THRESHOLD

# Layer precedence, highest first — mirrors SPEC.md §3. Index is rank;
# lower index always wins a same-topic collision against a higher index.
_LAYER_RANK = {layer: i for i, layer in enumerate(LAYERS)}

# Automatic promotion (confirm_node / consolidate) stops at branch, on
# purpose — it must never be able to reach a gated layer. A real bug found
# here during hardening: with "branch": "trunk" included, an unauthenticated
# leaf node ("trust me", authored_by="anyone", zero authorization anywhere)
# reached trunk after 4 plain confirmations, completely bypassing the
# authorization gate write_node enforces for direct trunk writes. Elevating
# a branch fact to trunk is only ever reachable through an explicit,
# authorized write_node() call to the same topic — which already works via
# the existing precedence rules (a trunk write supersedes a branch node on
# the same topic) — never through repetition alone. See SPEC.md §3.
_PROMOTE_TO = {"leaf": "branch"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ConflictError(Exception):
    """Raised when a write can't be reconciled automatically and needs a human.

    Same-layer topic collisions are never auto-resolved by recency — this is
    the code path that enforces that, per SPEC.md §3.
    """


class AuthorizationError(Exception):
    """Raised when a root/trunk write is missing authorized_by/confirmation."""


@dataclass
class Node:
    id: str
    layer: str
    topic: str
    content: str
    description: str = ""
    tags: list[str] = field(default_factory=list)
    created_at: str = ""
    last_confirmed_at: str = ""
    expires_at: str | None = None
    confirmations: int = 0
    authored_by: str = ""
    origin_machine: str = ""
    hash: str = ""
    prev_hash: str | None = None
    superseded_by: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Node":
        d = dict(row)
        d["tags"] = json.loads(d["tags"])
        return cls(**d)


def _advance(conn: sqlite3.Connection, node_id: str, prev_hash: str | None, diff: dict, actor: str) -> str:
    """The single hashing path for every state change to a node, ever.

    One formula, used identically by creation, confirmation, supersession,
    promotion, and pruning, is what makes verify_chain() possible at all —
    a chain that computed its hash differently per operation type couldn't
    be recomputed and checked generically. `diff` is serialized with
    sort_keys so the same logical diff always hashes the same way regardless
    of dict insertion order.
    """
    new_hash = hashlib.sha256(
        f"{prev_hash or ''}|{node_id}|{json.dumps(diff, sort_keys=True)}".encode("utf-8")
    ).hexdigest()
    conn.execute(
        "INSERT INTO edit_log (id, node_id, at, actor, diff, hash) VALUES (?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), node_id, _now(), actor, json.dumps(diff, sort_keys=True), new_hash),
    )
    return new_hash


def verify_chain(conn: sqlite3.Connection, node_id: str) -> dict:
    """Recompute a node's entire hash chain from its edit_log and check it
    against what's stored. This is what makes "provenance" mean something
    here rather than just being a hash column nobody ever checks — see
    SPEC.md §7. Detects: a tampered diff, a tampered hash, entries deleted
    or reordered, or a live node whose current hash doesn't match the end
    of its own logged history."""
    rows = conn.execute(
        "SELECT * FROM edit_log WHERE node_id = ? ORDER BY rowid ASC", (node_id,)
    ).fetchall()
    if not rows:
        return {"valid": False, "reason": "no edit_log entries for this node_id"}

    prev = None
    for i, row in enumerate(rows):
        diff = json.loads(row["diff"])
        expected = hashlib.sha256(
            f"{prev or ''}|{node_id}|{json.dumps(diff, sort_keys=True)}".encode("utf-8")
        ).hexdigest()
        if expected != row["hash"]:
            return {
                "valid": False,
                "reason": "hash mismatch — chain broken or tampered",
                "broken_at_entry_index": i,
                "edit_log_id": row["id"],
            }
        prev = row["hash"]

    current = conn.execute("SELECT hash FROM nodes WHERE id = ?", (node_id,)).fetchone()
    if current is not None and current["hash"] != prev:
        return {
            "valid": False,
            "reason": "live node's hash doesn't match the end of its own edit_log chain",
        }

    return {"valid": True, "entries_verified": len(rows)}


def write_node(
    conn: sqlite3.Connection,
    *,
    layer: str,
    topic: str,
    content: str,
    authored_by: str,
    origin_machine: str,
    description: str = "",
    tags: list[str] | None = None,
    leaf_ttl_days: int = 14,
    authorized_by: str | None = None,
    confirmation: bool = False,
) -> Node:
    """Write a new node. Root/trunk require authorized_by + confirmation=True
    — see SPEC.md §3/§5. Raises ConflictError on an unresolved same-layer
    topic collision rather than silently picking one."""
    if layer not in LAYERS:
        raise ValueError(f"unknown layer: {layer!r}")
    if layer in GATED_LAYERS and not (authorized_by and confirmation):
        raise AuthorizationError(
            f"writing to layer={layer!r} requires authorized_by and confirmation=True"
        )

    node_id = str(uuid.uuid4())
    now = _now()
    expires_at = (
        (datetime.now(timezone.utc) + timedelta(days=leaf_ttl_days)).isoformat()
        if layer == "leaf"
        else None
    )
    creation_diff = {"created": True, "layer": layer, "topic": topic, "content": content}

    existing = conn.execute(
        "SELECT * FROM nodes WHERE topic = ? AND superseded_by IS NULL", (topic,)
    ).fetchall()

    superseded_by = None
    for row in existing:
        other = Node.from_row(row)
        if other.layer == layer:
            # Same-layer collision — never auto-resolved by recency (SPEC.md §3).
            # Deferred until after insert below (node_id must exist for the FK).
            pass
        elif _LAYER_RANK[layer] < _LAYER_RANK[other.layer]:
            pass  # new node outranks the existing one — handled after insert below
        else:
            # Existing node already outranks this new, lower-layer write.
            superseded_by = other.id

    # Genesis hash for this node — logged to edit_log before the node row
    # exists (edit_log has no FK on node_id precisely so it can outlive or
    # precede the row it describes; see schema.py).
    chain_hash = _advance(conn, node_id, None, creation_diff, authored_by)

    conn.execute(
        """INSERT INTO nodes (id, layer, topic, content, description, tags,
               created_at, last_confirmed_at, expires_at, confirmations,
               authored_by, origin_machine, hash, prev_hash, superseded_by)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            node_id, layer, topic, content, description, json.dumps(tags or []),
            now, now, expires_at, 0,
            authored_by, origin_machine, chain_hash, None, superseded_by,
        ),
    )

    for row in existing:
        other = Node.from_row(row)
        if other.layer == layer:
            conn.execute(
                "INSERT INTO conflicts (id, topic, node_a, node_b, detected_at) VALUES (?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), topic, other.id, node_id, now),
            )
        elif _LAYER_RANK[layer] < _LAYER_RANK[other.layer]:
            # New node outranks the existing one — existing one is superseded.
            # This IS a real state change to `other`, so its chain advances
            # too — a supersede event that never touched the hash was the
            # bug this refactor exists to fix.
            other_new_hash = _advance(conn, other.id, other.hash, {"superseded_by": node_id}, authored_by)
            conn.execute(
                "UPDATE nodes SET superseded_by = ?, hash = ?, prev_hash = ? WHERE id = ?",
                (node_id, other_new_hash, other.hash, other.id),
            )

    conn.commit()

    return Node.from_row(conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone())


def confirm_node(conn: sqlite3.Connection, node_id: str, actor: str) -> Node:
    """Reconfirm a node. On a leaf node, this both pushes its expiry out and
    counts toward promotion — see SPEC.md §3 (the actual learning mechanism:
    a fact earns a permanent home by surviving repeated contact, not by
    being asserted once)."""
    row = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
    if row is None:
        raise KeyError(f"no such node: {node_id}")
    node = Node.from_row(row)

    confirmations = node.confirmations + 1
    now = _now()
    new_layer = node.layer
    expires_at = node.expires_at

    if node.layer in _PROMOTE_TO and confirmations >= DEFAULT_PROMOTION_THRESHOLD:
        new_layer = _PROMOTE_TO[node.layer]
        expires_at = None  # promoted nodes no longer decay
    elif node.layer == "leaf":
        expires_at = (datetime.now(timezone.utc) + timedelta(days=14)).isoformat()

    diff = {"confirmations": confirmations, "layer": new_layer, "promoted": new_layer != node.layer}
    chain_hash = _advance(conn, node.id, node.hash, diff, actor)

    conn.execute(
        """UPDATE nodes SET confirmations = ?, last_confirmed_at = ?, layer = ?,
               expires_at = ?, hash = ?, prev_hash = ? WHERE id = ?""",
        (confirmations, now, new_layer, expires_at, chain_hash, node.hash, node.id),
    )
    conn.commit()
    return Node.from_row(conn.execute("SELECT * FROM nodes WHERE id = ?", (node.id,)).fetchone())


def recall(conn: sqlite3.Connection, query: str = "", layers: list[str] | None = None) -> list[Node]:
    """Root and trunk always load in full. Branch and leaf are relevance-
    filtered by a plain keyword match — cheap, deterministic, no external
    dependency. See SPEC.md §6: if this ever degrades, it degrades to
    root+trunk only, never to nothing."""
    wanted = layers or list(LAYERS)
    results: list[Node] = []

    if "root" in wanted or "trunk" in wanted:
        rows = conn.execute(
            "SELECT * FROM nodes WHERE layer IN ('root','trunk') AND superseded_by IS NULL"
        ).fetchall()
        results.extend(Node.from_row(r) for r in rows if r["layer"] in wanted)

    if query and ("branch" in wanted or "leaf" in wanted):
        fts_query = _fts_match_query(query)
        if fts_query:
            # Written as `rowid IN (subquery)` rather than a JOIN — measured
            # directly (scripts/scale_test.py): a JOIN lets SQLite's planner
            # pick "scan every branch/leaf row, probe FTS5 per row" instead
            # of "search the FTS5 index first" — 285ms vs 6.6ms at 5,000
            # nodes for the exact same query. The subquery form reliably
            # forces the index search to run first.
            rows = conn.execute(
                """SELECT * FROM nodes
                       WHERE rowid IN (SELECT rowid FROM nodes_fts WHERE nodes_fts MATCH ?)
                         AND layer IN ('branch','leaf')
                         AND superseded_by IS NULL
                         AND (expires_at IS NULL OR expires_at > ?)""",
                (fts_query, _now()),
            ).fetchall()
            results.extend(Node.from_row(r) for r in rows if r["layer"] in wanted)

    return results


def _fts_match_query(query: str) -> str:
    """Turn free text into a safe FTS5 MATCH expression: each term quoted as
    a literal phrase (so FTS5 operator characters in the input, e.g. a stray
    `*` or `:`, can't produce a syntax error or an unintended query), joined
    with OR (matching any term, same recall behavior as the old substring
    scan). Empty/whitespace-only input yields no query at all."""
    terms = [t for t in query.split() if t]
    if not terms:
        return ""
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)


def consolidate(conn: sqlite3.Connection, actor: str = "system") -> dict:
    """The decay/promotion sweep. Prunes expired, never-reconfirmed leaf
    nodes; promotes anything that crossed the threshold outside of an
    explicit confirm_node call. Every prune is logged before the row is
    removed — the edit_log stays the durable record of what existed and why
    it left, even once the live node is gone."""
    now = _now()
    pruned, promoted = [], []

    expired = conn.execute(
        "SELECT * FROM nodes WHERE layer = 'leaf' AND expires_at IS NOT NULL AND expires_at <= ? AND confirmations < ?",
        (now, DEFAULT_PROMOTION_THRESHOLD),
    ).fetchall()
    for row in expired:
        node = Node.from_row(row)
        _advance(conn, node.id, node.hash, {"pruned_expired": True}, actor)
        conn.execute("DELETE FROM nodes WHERE id = ?", (node.id,))
        pruned.append(node.id)

    # layer = 'leaf' explicitly, not "in _PROMOTE_TO", so this query stays
    # correct on its own even if _PROMOTE_TO's shape ever changes again.
    stale_promotable = conn.execute(
        "SELECT * FROM nodes WHERE layer = 'leaf' AND confirmations >= ?",
        (DEFAULT_PROMOTION_THRESHOLD,),
    ).fetchall()
    for row in stale_promotable:
        node = Node.from_row(row)
        if node.layer not in _PROMOTE_TO:
            continue
        new_layer = _PROMOTE_TO[node.layer]
        chain_hash = _advance(conn, node.id, node.hash, {"promoted_to": new_layer}, actor)
        conn.execute(
            "UPDATE nodes SET layer = ?, expires_at = NULL, hash = ?, prev_hash = ? WHERE id = ?",
            (new_layer, chain_hash, node.hash, node.id),
        )
        promoted.append({"id": node.id, "to": new_layer})

    conn.commit()
    return {"pruned": pruned, "promoted": promoted}


def list_conflicts(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM conflicts WHERE resolved_at IS NULL").fetchall()
    return [dict(r) for r in rows]


def resolve_conflict(conn: sqlite3.Connection, conflict_id: str, resolution: str) -> None:
    """Raises KeyError for an unknown conflict_id rather than silently
    no-op'ing — found during hardening: the UPDATE below "succeeds" (0 rows
    affected is not an error to SQLite) whether or not conflict_id is real,
    so a typo'd or already-resolved ID would otherwise get reported back to
    a caller as {"resolved": true}, which is actively false."""
    cur = conn.execute(
        "UPDATE conflicts SET resolved_at = ?, resolution = ? WHERE id = ? AND resolved_at IS NULL",
        (_now(), resolution, conflict_id),
    )
    if cur.rowcount == 0:
        exists = conn.execute("SELECT 1 FROM conflicts WHERE id = ?", (conflict_id,)).fetchone()
        reason = "already resolved" if exists else "no such conflict"
        raise KeyError(f"cannot resolve conflict {conflict_id!r}: {reason}")
    conn.commit()
