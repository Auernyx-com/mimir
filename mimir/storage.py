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

_PROMOTE_TO = {"leaf": "branch", "branch": "trunk"}


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


def _hash_of(*parts: str) -> str:
    return hashlib.sha256("|".join(p or "" for p in parts).encode("utf-8")).hexdigest()


def _log_edit(conn: sqlite3.Connection, node_id: str, actor: str, diff: dict, chain_hash: str) -> None:
    conn.execute(
        "INSERT INTO edit_log (id, node_id, at, actor, diff, hash) VALUES (?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), node_id, _now(), actor, json.dumps(diff), chain_hash),
    )


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
    chain_hash = _hash_of(node_id, layer, topic, content, "")  # prev_hash="" — genesis for this node

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

    # Insert first so any FK reference to node_id (conflicts, superseded_by) is valid.
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
    _log_edit(conn, node_id, authored_by, {"created": True, "layer": layer, "topic": topic}, chain_hash)

    for row in existing:
        other = Node.from_row(row)
        if other.layer == layer:
            conn.execute(
                "INSERT INTO conflicts (id, topic, node_a, node_b, detected_at) VALUES (?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), topic, other.id, node_id, now),
            )
        elif _LAYER_RANK[layer] < _LAYER_RANK[other.layer]:
            # New node outranks the existing one — existing one is superseded.
            conn.execute("UPDATE nodes SET superseded_by = ? WHERE id = ?", (node_id, other.id))
            _log_edit(conn, other.id, authored_by, {"superseded_by": node_id}, other.hash)

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

    chain_hash = _hash_of(node.id, "confirm", str(confirmations), node.hash)

    conn.execute(
        """UPDATE nodes SET confirmations = ?, last_confirmed_at = ?, layer = ?,
               expires_at = ?, hash = ?, prev_hash = ? WHERE id = ?""",
        (confirmations, now, new_layer, expires_at, chain_hash, node.hash, node.id),
    )
    _log_edit(
        conn, node.id, actor,
        {"confirmations": confirmations, "layer": new_layer, "promoted": new_layer != node.layer},
        chain_hash,
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
        terms = [t.lower() for t in query.split() if t]
        rows = conn.execute(
            """SELECT * FROM nodes WHERE layer IN ('branch','leaf') AND superseded_by IS NULL
                   AND (expires_at IS NULL OR expires_at > ?)""",
            (_now(),),
        ).fetchall()
        for r in rows:
            if r["layer"] not in wanted:
                continue
            haystack = f"{r['topic']} {r['description']} {r['content']} {r['tags']}".lower()
            if any(term in haystack for term in terms):
                results.append(Node.from_row(r))

    return results


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
        _log_edit(conn, node.id, actor, {"pruned_expired": True}, node.hash)
        conn.execute("DELETE FROM nodes WHERE id = ?", (node.id,))
        pruned.append(node.id)

    stale_promotable = conn.execute(
        "SELECT * FROM nodes WHERE layer IN ('leaf','branch') AND confirmations >= ?",
        (DEFAULT_PROMOTION_THRESHOLD,),
    ).fetchall()
    for row in stale_promotable:
        node = Node.from_row(row)
        if node.layer not in _PROMOTE_TO:
            continue
        new_layer = _PROMOTE_TO[node.layer]
        chain_hash = _hash_of(node.id, "consolidate_promote", new_layer, node.hash)
        conn.execute(
            "UPDATE nodes SET layer = ?, expires_at = NULL, hash = ?, prev_hash = ? WHERE id = ?",
            (new_layer, chain_hash, node.hash, node.id),
        )
        _log_edit(conn, node.id, actor, {"promoted_to": new_layer}, chain_hash)
        promoted.append({"id": node.id, "to": new_layer})

    conn.commit()
    return {"pruned": pruned, "promoted": promoted}


def list_conflicts(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM conflicts WHERE resolved_at IS NULL").fetchall()
    return [dict(r) for r in rows]


def resolve_conflict(conn: sqlite3.Connection, conflict_id: str, resolution: str) -> None:
    conn.execute(
        "UPDATE conflicts SET resolved_at = ?, resolution = ? WHERE id = ?",
        (_now(), resolution, conflict_id),
    )
    conn.commit()
