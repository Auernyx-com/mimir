"""Sanity tests for the core storage mechanics — run with: python -m pytest tests/
or, dependency-free: python tests/test_storage.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir import schema, storage  # noqa: E402


def _fresh_conn():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    return schema.connect(tmp.name)


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


def test_consolidate_prunes_expired_unconfirmed_leaf():
    conn = _fresh_conn()
    node = storage.write_node(
        conn, layer="leaf", topic="temp.fact", content="short-lived",
        authored_by="test", origin_machine="test-machine", leaf_ttl_days=-1,  # already expired
    )
    result = storage.consolidate(conn)
    assert node.id in result["pruned"]
    remaining = storage.recall(conn, query="temp", layers=["leaf"])
    assert all(n.id != node.id for n in remaining)


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
