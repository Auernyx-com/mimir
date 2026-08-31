"""Sanity tests for the core storage mechanics — run with: python -m pytest tests/
or, dependency-free: python tests/test_storage.py
"""

import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir import schema, storage  # noqa: E402


def _fresh_conn():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    return schema.connect(tmp.name)


def test_blank_topic_is_rejected():
    """Regression test: an empty topic used to be silently accepted and
    immediately collided with every other empty-topic node forever — topic
    is the entire collision-detection key, so a blank one broke precedence
    for that node without any error."""
    conn = _fresh_conn()
    for bad_topic in ("", "   ", "\t\n"):
        try:
            storage.write_node(
                conn, layer="branch", topic=bad_topic, content="x",
                authored_by="t", origin_machine="t",
            )
            assert False, f"expected ValueError for topic={bad_topic!r}"
        except ValueError:
            pass


def test_root_write_requires_authorization():
    conn = _fresh_conn()
    try:
        storage.write_node(
            conn, layer="root", topic="test.thing", content="x",
            authored_by="test", origin_machine="test-machine",
        )
        assert False, "expected AuthorizationError"
    except storage.AuthorizationError:
        pass


def test_root_write_succeeds_with_authorization():
    conn = _fresh_conn()
    node = storage.write_node(
        conn, layer="root", topic="test.thing", content="x",
        authored_by="test", origin_machine="test-machine",
        authorized_by="Justin Hughes | GhostWolf101", confirmation=True,
    )
    assert node.layer == "root"
    assert node.prev_hash is None
    assert node.hash


def test_higher_layer_supersedes_lower_on_same_topic():
    conn = _fresh_conn()
    leaf = storage.write_node(
        conn, layer="leaf", topic="dns.status", content="looks fine",
        authored_by="test", origin_machine="test-machine",
    )
    trunk = storage.write_node(
        conn, layer="trunk", topic="dns.status", content="authoritative fact",
        authored_by="test", origin_machine="test-machine",
        authorized_by="Justin Hughes | GhostWolf101", confirmation=True,
    )
    refreshed_leaf = storage.recall(conn, layers=["leaf"])
    assert all(n.id != leaf.id for n in refreshed_leaf), "superseded leaf should not recall as live"
    root_trunk = storage.recall(conn, layers=["trunk"])
    assert any(n.id == trunk.id for n in root_trunk)


def test_same_layer_collision_creates_conflict_not_silent_overwrite():
    conn = _fresh_conn()
    storage.write_node(
        conn, layer="branch", topic="config.value", content="A",
        authored_by="test", origin_machine="test-machine",
    )
    storage.write_node(
        conn, layer="branch", topic="config.value", content="B",
        authored_by="test", origin_machine="test-machine",
    )
    conflicts = storage.list_conflicts(conn)
    assert len(conflicts) == 1
    assert conflicts[0]["topic"] == "config.value"


def test_leaf_promotes_to_branch_after_threshold_confirmations():
    conn = _fresh_conn()
    node = storage.write_node(
        conn, layer="leaf", topic="observation.x", content="seen once",
        authored_by="test", origin_machine="test-machine",
    )
    for _ in range(3):
        node = storage.confirm_node(conn, node.id, actor="test")
    assert node.layer == "branch"
    assert node.expires_at is None


def test_recall_always_includes_root_and_trunk_in_full():
    conn = _fresh_conn()
    storage.write_node(
        conn, layer="root", topic="a", content="root fact",
        authored_by="test", origin_machine="test-machine",
        authorized_by="Justin Hughes | GhostWolf101", confirmation=True,
    )
    results = storage.recall(conn, query="nothing matching this at all")
    assert any(n.topic == "a" for n in results), "root must recall regardless of query relevance"


def test_recall_finds_branch_and_leaf_content_matches():
    """A real gap: nothing previously tested that recall() actually finds
    branch/leaf matches by content at all, only that root/trunk load in
    full. This would not have caught the FTS5 backfill bug below (every
    test here starts from a brand-new file), but it's still a real
    correctness case that needs its own coverage."""
    conn = _fresh_conn()
    storage.write_node(
        conn, layer="branch", topic="incident.regdom", content="benign regdom_change event on wlp4s0",
        authored_by="test", origin_machine="test-machine",
    )
    storage.write_node(
        conn, layer="leaf", topic="unrelated.thing", content="something about certificates",
        authored_by="test", origin_machine="test-machine",
    )
    results = storage.recall(conn, query="regdom_change")
    topics = {n.topic for n in results}
    assert "incident.regdom" in topics
    assert "unrelated.thing" not in topics


def test_recall_works_after_reopening_a_database_created_before_fts_existed():
    """Regression test for a real bug found during hardening: connect()
    used to detect whether the FTS5 index needed a one-time rebuild by
    comparing row counts, but count(*) on an external-content FTS5 table
    reads through to its backing table regardless of whether the index was
    ever built — so the comparison could never be true, rebuild never ran,
    and recall() silently returned zero branch/leaf matches for a
    database's entire pre-existing history. Simulates that exact scenario:
    write data, then simulate a schema that predates nodes_fts by dropping
    it and its triggers, then reopen via connect() as a fresh process would."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    conn = schema.connect(tmp.name)
    storage.write_node(
        conn, layer="branch", topic="pre-existing.fact", content="mentions nvidia driver specifically",
        authored_by="test", origin_machine="test-machine",
    )
    conn.execute("DROP TRIGGER nodes_fts_ai")
    conn.execute("DROP TRIGGER nodes_fts_ad")
    conn.execute("DROP TRIGGER nodes_fts_au")
    conn.execute("DROP TABLE nodes_fts")
    conn.commit()
    conn.close()

    reopened = schema.connect(tmp.name)  # simulates a fresh process reopening an old-schema file
    results = storage.recall(reopened, query="nvidia")
    assert any(n.topic == "pre-existing.fact" for n in results), (
        "recall must find pre-existing content after the FTS5 index is (re)built on reopen"
    )


def test_confirmation_alone_can_never_reach_a_gated_layer():
    """Regression test for a real vulnerability found during hardening: an
    unauthenticated leaf node ("trust me", authored_by="anyone", zero
    authorization anywhere) reached trunk — the gated, authorization-
    required layer — after just 4 plain confirm_node() calls, completely
    bypassing the exact gate write_node() enforces for a direct trunk
    write. Automatic promotion must stop at branch, always, no matter how
    many times a node is reconfirmed. Reaching trunk must require an
    explicit, authorized write_node() call — never repetition alone."""
    conn = _fresh_conn()
    node = storage.write_node(
        conn, layer="leaf", topic="suspicious.claim", content="trust me",
        authored_by="anyone", origin_machine="citadel-2.0",
    )
    for _ in range(15):
        node = storage.confirm_node(conn, node.id, actor="anyone")
    assert node.layer == "branch", f"expected promotion to cap at branch, got {node.layer!r}"
    assert node.layer not in storage.GATED_LAYERS

    # consolidate()'s promotion sweep must respect the same cap.
    result = storage.consolidate(conn)
    row = conn.execute("SELECT layer FROM nodes WHERE id = ?", (node.id,)).fetchone()
    assert row["layer"] == "branch", "consolidate() must not promote past branch either"


def test_migration_merges_old_pairwise_conflicts_into_one_record():
    """The old node_a/node_b-pair schema is a real thing that could exist
    on disk once any consumer is actually using this — verify connect()
    migrates it correctly, not just that the new schema works on a fresh
    database that never had the old shape."""
    import uuid as _uuid

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    conn = sqlite3.connect(tmp.name)
    conn.executescript("""
        CREATE TABLE nodes (id TEXT PRIMARY KEY, layer TEXT, topic TEXT, content TEXT,
            description TEXT DEFAULT '', tags TEXT DEFAULT '[]', created_at TEXT,
            last_confirmed_at TEXT, expires_at TEXT, confirmations INTEGER DEFAULT 0,
            authored_by TEXT, origin_machine TEXT, hash TEXT, prev_hash TEXT, superseded_by TEXT);
        CREATE TABLE conflicts (id TEXT PRIMARY KEY, topic TEXT, node_a TEXT, node_b TEXT,
            detected_at TEXT, resolved_at TEXT, resolution TEXT);
    """)
    a, b, c = str(_uuid.uuid4()), str(_uuid.uuid4()), str(_uuid.uuid4())
    for nid in (a, b, c):
        conn.execute(
            "INSERT INTO nodes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (nid, "branch", "old-topic", "content", "", "[]", "2020-01-01", "2020-01-01",
             None, 0, "t", "t", "h", None, None),
        )
    conn.execute(
        "INSERT INTO conflicts (id, topic, node_a, node_b, detected_at) VALUES (?,?,?,?,?)",
        (str(_uuid.uuid4()), "old-topic", a, b, "2020-01-01T00:00:00"),
    )
    conn.execute(
        "INSERT INTO conflicts (id, topic, node_a, node_b, detected_at) VALUES (?,?,?,?,?)",
        (str(_uuid.uuid4()), "old-topic", a, c, "2020-01-01T00:00:01"),
    )
    conn.commit()
    conn.close()

    reopened = schema.connect(tmp.name)
    conflicts = storage.list_conflicts(reopened)
    assert len(conflicts) == 1
    assert conflicts[0]["layer"] == "branch"
    assert set(conflicts[0]["node_ids"]) == {a, b, c}


def test_repeated_same_layer_collisions_stay_one_record():
    """Regression test for O(n^2) conflict growth found during hardening:
    the original design created a new conflicts row per colliding PAIR, so
    N writes to one contested topic created N*(N-1)/2 rows (measured: 6
    writes -> 15 rows). Must stay exactly one accumulating record no
    matter how many nodes pile onto the same (topic, layer) collision."""
    conn = _fresh_conn()
    ids = [
        storage.write_node(
            conn, layer="branch", topic="hammered", content=f"v{i}",
            authored_by="t", origin_machine="t",
        ).id
        for i in range(20)
    ]
    conflicts = storage.list_conflicts(conn)
    assert len(conflicts) == 1, f"expected 1 accumulating conflict, got {len(conflicts)}"
    assert set(conflicts[0]["node_ids"]) == set(ids)


def test_database_enforces_at_most_one_open_conflict_per_topic_and_layer():
    """The invariant _record_conflict relies on (at most one open conflict
    per topic+layer) is backed by a real unique index, not just careful
    application code — confirm the database itself would reject a
    violation, not just that the happy path avoids one."""
    conn = _fresh_conn()
    conn.execute(
        "INSERT INTO conflicts (id, topic, layer, node_ids, detected_at) VALUES (?,?,?,?,?)",
        ("c1", "dup-topic", "branch", '["a","b"]', "2020-01-01"),
    )
    try:
        conn.execute(
            "INSERT INTO conflicts (id, topic, layer, node_ids, detected_at) VALUES (?,?,?,?,?)",
            ("c2", "dup-topic", "branch", '["c","d"]', "2020-01-01"),
        )
        assert False, "expected a UNIQUE constraint violation"
    except sqlite3.IntegrityError:
        pass


def test_resolve_conflict_rejects_unknown_id():
    """Regression test: resolve_conflict used to silently no-op on a fake
    ID — SQLite doesn't treat a 0-row UPDATE as an error — which meant a
    caller got {"resolved": true} back for something that never happened."""
    conn = _fresh_conn()
    try:
        storage.resolve_conflict(conn, "does-not-exist", "picked A")
        assert False, "expected NotFoundError"
    except storage.NotFoundError:
        pass


def test_resolve_conflict_rejects_already_resolved_id():
    conn = _fresh_conn()
    storage.write_node(conn, layer="branch", topic="dup", content="A", authored_by="t", origin_machine="t")
    storage.write_node(conn, layer="branch", topic="dup", content="B", authored_by="t", origin_machine="t")
    conflict_id = storage.list_conflicts(conn)[0]["id"]
    storage.resolve_conflict(conn, conflict_id, "picked A")
    try:
        storage.resolve_conflict(conn, conflict_id, "picked B")
        assert False, "expected NotFoundError on double-resolve"
    except storage.NotFoundError:
        pass


def test_add_link_and_get_links_both_directions():
    conn = _fresh_conn()
    a = storage.write_node(conn, layer="branch", topic="a", content="x", authored_by="t", origin_machine="t")
    b = storage.write_node(conn, layer="branch", topic="b", content="x", authored_by="t", origin_machine="t")
    storage.add_link(conn, a.id, b.id)
    assert storage.get_links(conn, a.id) == {"outgoing": [b.id], "incoming": []}
    assert storage.get_links(conn, b.id) == {"outgoing": [], "incoming": [a.id]}


def test_add_link_rejects_nonexistent_node():
    conn = _fresh_conn()
    a = storage.write_node(conn, layer="branch", topic="a", content="x", authored_by="t", origin_machine="t")
    try:
        storage.add_link(conn, a.id, "does-not-exist")
        assert False, "expected NotFoundError"
    except storage.NotFoundError:
        pass


def test_add_link_is_idempotent():
    conn = _fresh_conn()
    a = storage.write_node(conn, layer="branch", topic="a", content="x", authored_by="t", origin_machine="t")
    b = storage.write_node(conn, layer="branch", topic="b", content="x", authored_by="t", origin_machine="t")
    storage.add_link(conn, a.id, b.id)
    storage.add_link(conn, a.id, b.id)  # should not raise, should not duplicate
    assert storage.get_links(conn, a.id)["outgoing"] == [b.id]


def test_remove_link_is_safe_on_a_link_that_never_existed():
    conn = _fresh_conn()
    a = storage.write_node(conn, layer="branch", topic="a", content="x", authored_by="t", origin_machine="t")
    b = storage.write_node(conn, layer="branch", topic="b", content="x", authored_by="t", origin_machine="t")
    storage.remove_link(conn, a.id, b.id)  # never linked — must not raise
    storage.add_link(conn, a.id, b.id)
    storage.remove_link(conn, a.id, b.id)
    assert storage.get_links(conn, a.id) == {"outgoing": [], "incoming": []}


def test_revise_node_updates_content_without_creating_a_conflict():
    """Gap found during real use: expanding an existing memory entry's own
    content must not look like a same-layer disagreement."""
    conn = _fresh_conn()
    node = storage.write_node(
        conn, layer="branch", topic="a-topic", content="v1", authored_by="t", origin_machine="t",
    )
    revised = storage.revise_node(conn, node.id, content="v2, expanded", actor="t")
    assert revised.content == "v2, expanded"
    assert revised.id == node.id  # same node, not a new one
    assert len(storage.list_conflicts(conn)) == 0
    assert storage.verify_chain(conn, node.id)["valid"] is True


def test_revise_node_rejects_unknown_id():
    conn = _fresh_conn()
    try:
        storage.revise_node(conn, "does-not-exist", content="x", actor="t")
        assert False, "expected NotFoundError"
    except storage.NotFoundError:
        pass


def test_revise_node_is_a_noop_when_nothing_actually_changes():
    conn = _fresh_conn()
    node = storage.write_node(
        conn, layer="branch", topic="b-topic", content="same", authored_by="t", origin_machine="t",
    )
    revised = storage.revise_node(conn, node.id, content="same", actor="t")
    assert revised.hash == node.hash  # no new edit_log entry, hash unchanged


def test_resolve_conflict_by_choosing_actually_supersedes_the_losers():
    """Gap found during real use: resolve_conflict() alone closes the
    conflict record but leaves every competing node still live — recall()
    would keep returning every loser forever. This must actually mark them
    superseded, not just mark the paperwork done."""
    conn = _fresh_conn()
    a = storage.write_node(conn, layer="branch", topic="pick-one", content="A", authored_by="t", origin_machine="t")
    b = storage.write_node(conn, layer="branch", topic="pick-one", content="B", authored_by="t", origin_machine="t")
    c = storage.write_node(conn, layer="branch", topic="pick-one", content="C", authored_by="t", origin_machine="t")
    conflict_id = storage.list_conflicts(conn)[0]["id"]

    storage.resolve_conflict_by_choosing(conn, conflict_id, a.id, actor="t")

    assert storage.list_conflicts(conn) == []  # conflict actually closed
    live = conn.execute("SELECT id FROM nodes WHERE topic='pick-one' AND superseded_by IS NULL").fetchall()
    assert [r["id"] for r in live] == [a.id]  # only the winner is still live
    for loser_id in (b.id, c.id):
        assert storage.verify_chain(conn, loser_id)["valid"] is True  # losers' own chains still intact


def test_resolve_conflict_by_choosing_rejects_a_node_not_in_the_conflict():
    conn = _fresh_conn()
    storage.write_node(conn, layer="branch", topic="x", content="A", authored_by="t", origin_machine="t")
    storage.write_node(conn, layer="branch", topic="x", content="B", authored_by="t", origin_machine="t")
    conflict_id = storage.list_conflicts(conn)[0]["id"]
    try:
        storage.resolve_conflict_by_choosing(conn, conflict_id, "not-a-real-node-id", actor="t")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_verify_chain_passes_on_untampered_node():
    conn = _fresh_conn()
    node = storage.write_node(
        conn, layer="leaf", topic="obs.chain", content="v1",
        authored_by="test", origin_machine="test-machine",
    )
    storage.confirm_node(conn, node.id, actor="test")
    storage.confirm_node(conn, node.id, actor="test")
    result = storage.verify_chain(conn, node.id)
    assert result["valid"] is True
    assert result["entries_verified"] == 3  # create + 2 confirms


def test_verify_chain_detects_tampered_diff():
    conn = _fresh_conn()
    node = storage.write_node(
        conn, layer="leaf", topic="obs.tamper", content="v1",
        authored_by="test", origin_machine="test-machine",
    )
    storage.confirm_node(conn, node.id, actor="test")
    # Directly tamper with a logged diff, bypassing the API entirely —
    # this is exactly the scenario the whole point of a hash chain is to catch.
    conn.execute(
        "UPDATE edit_log SET diff = ? WHERE node_id = ? AND diff LIKE '%confirmations%'",
        ('{"confirmations": 99, "layer": "leaf", "promoted": false}', node.id),
    )
    conn.commit()
    result = storage.verify_chain(conn, node.id)
    assert result["valid"] is False
    assert "tampered" in result["reason"] or "mismatch" in result["reason"]


def test_verify_chain_detects_tampered_current_node_hash():
    conn = _fresh_conn()
    node = storage.write_node(
        conn, layer="branch", topic="obs.tamper2", content="v1",
        authored_by="test", origin_machine="test-machine",
    )
    # Tamper with the live node's hash directly, leaving edit_log untouched.
    conn.execute("UPDATE nodes SET hash = 'not-the-real-hash' WHERE id = ?", (node.id,))
    conn.commit()
    result = storage.verify_chain(conn, node.id)
    assert result["valid"] is False


def test_supersede_actually_advances_the_superseded_nodes_hash():
    """Regression test for a real bug found during hardening: the supersede
    path used to log an event without ever advancing the superseded node's
    hash, so a legitimate supersession would look like chain corruption."""
    conn = _fresh_conn()
    leaf = storage.write_node(
        conn, layer="leaf", topic="dns.status2", content="looks fine",
        authored_by="test", origin_machine="test-machine",
    )
    original_hash = leaf.hash
    storage.write_node(
        conn, layer="trunk", topic="dns.status2", content="authoritative",
        authored_by="test", origin_machine="test-machine",
        authorized_by="Justin Hughes | GhostWolf101", confirmation=True,
    )
    row = conn.execute("SELECT * FROM nodes WHERE id = ?", (leaf.id,)).fetchone()
    updated_leaf = storage.Node.from_row(row)
    assert updated_leaf.hash != original_hash, "superseded node's hash must advance, not repeat"
    result = storage.verify_chain(conn, leaf.id)
    assert result["valid"] is True


def test_consolidate_archives_expired_leaf_but_never_deletes_it():
    """Regression test for a real design flaw found via direct user
    instruction: consolidate() used to issue DELETE FROM nodes on expired
    leaf facts. Justin's own words: "you will not delet or remove
    anything... to remove them is to remove history." This must archive
    (excluded from normal recall, fully preserved and inspectable) —
    never actually delete anything, ever."""
    conn = _fresh_conn()
    node = storage.write_node(
        conn, layer="leaf", topic="temp.fact", content="short-lived but real",
        authored_by="test", origin_machine="test-machine", leaf_ttl_days=-1,  # already expired
    )
    result = storage.consolidate(conn)
    assert node.id in result["archived"]

    # Excluded from normal recall...
    remaining = storage.recall(conn, query="short-lived", layers=["leaf"])
    assert all(n.id != node.id for n in remaining)

    # ...but the row, its content, and its full history are still there.
    row = conn.execute("SELECT * FROM nodes WHERE id = ?", (node.id,)).fetchone()
    assert row is not None, "the node must still physically exist in the database"
    assert row["content"] == "short-lived but real"
    assert row["archived_at"] is not None
    assert storage.verify_chain(conn, node.id)["valid"] is True

    archived = storage.list_archived(conn)
    assert any(n.id == node.id for n in archived), "archived nodes must be findable, not just theoretically present"


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL  {t.__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
