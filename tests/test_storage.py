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
