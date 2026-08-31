# Mimir

> "Many trunks, one root system."

A memory **framework**, not a memory store. One schema, one API, one set of
root/trunk/branch/leaf mechanics — and every consumer instantiates its own
fully isolated copy from it, with zero data crossover between instances.

Full design rationale: **[SPEC.md](SPEC.md)**.

## Quickstart

No dependencies beyond the Python standard library.

```bash
# Start one instance (one database file = one consumer's isolated memory)
python -m mimir.server --db ./my-instance.db --port 8420

# Write a fact (branch/leaf — no authorization needed)
curl -s -X POST localhost:8420/write -d '{
  "layer": "leaf", "topic": "obs.example", "content": "saw this once",
  "authored_by": "my-program", "origin_machine": "citadel-2.0"
}'

# Root/trunk writes require authorization
curl -s -X POST localhost:8420/write -d '{
  "layer": "root", "topic": "law.example", "content": "a standing fact",
  "authored_by": "my-program", "origin_machine": "citadel-2.0",
  "authorized_by": "Justin Hughes | GhostWolf101", "confirmation": true
}'

# Recall — root/trunk always come back in full; branch/leaf are query-filtered
curl -s -X POST localhost:8420/recall -d '{"query": "saw"}'

# Reconfirm a leaf fact — 3 reconfirmations promotes it to branch
curl -s -X POST localhost:8420/confirm -d '{"node_id": "<id from write above>"}'

# Sweep: prune expired unconfirmed leaf nodes, promote anything that crossed threshold
curl -s -X POST localhost:8420/consolidate -d '{}'
```

## Running the tests

```bash
python tests/test_storage.py
# or, if pytest is installed:
python -m pytest tests/
```

## One instance per consumer — always

Nothing in this codebase ever points two consumers at the same database
file, and nothing ever reads a SQLite file directly except through this
package's own `mimir.storage` functions. That's not an implementation
detail — it's the entire point. See [SPEC.md §2 and §8](SPEC.md) for why.

## Status

v0.1 — core storage mechanics (authorization-gated root/trunk, per-node
hash-chained provenance, topic-collision conflict detection, leaf decay and
promotion, tag-based recall) implemented and tested. HTTP daemon is a thin,
stdlib-only wrapper over the same storage layer.

Not yet built: cross-instance sync (opt-in, per-instance, see SPEC.md §10),
an embedding-based recall backend (v2, gated on a documented offline
fallback), and real consumer integrations (Mk2, Feneris, Skjoldr).

## Naming

"Mimir" is a placeholder — see [SPEC.md](SPEC.md#naming).
