# Mimir — Memory Framework

**Status:** v0.1, draft.

Not a shared memory store. A reusable **framework**: one schema, one API, one
set of layer/consolidation/provenance mechanics — instantiated separately and
fully isolated per consumer, instead of every AI-touching program inventing
its own bespoke memory from scratch.

## 1. Problem statement

Claude Code's own memory works today — a flat directory of markdown files,
each tagged `root/trunk/branch/leaf`, loaded wholesale each session. It works
because there are a dozen files, and because it's scoped to one AI. That
pattern is worth keeping. Copying its *content* anywhere else would be
exactly the wrong move — see [Scope note](#scope-note).

Three separate problems, one root cause:

- **Every AI-touching program either has no durable memory, or invents its
  own bespoke, non-reusable version.** Mk2 (Kotlin), Feneris (Python),
  Skjoldr (Python + PowerShell), and any AVRS deployment each end up solving
  "how do I remember things" from scratch, differently, every time.
- **Local LLM experiments keep re-learning from zero.** Every session starts
  cold — no durable recall across resets, no consolidation of what proved
  true last time.
- **Recall doesn't scale on ad hoc storage.** "Load everything and hope the
  index is enough" works at a dozen files in one program's own memory. It's
  not a strategy for anything bigger — but the fix is a design that scales
  per-instance, not a bigger shared pile.

Mimir is a framework: one schema, one API, one set of mechanics — and every
consumer spins up its **own**, fully isolated instance from it.

## 2. Scope note

My (Claude Code's) own memory stays mine — a possible future Mimir instance,
never a shared one. AVRS running for a financial client and AVRS running for
a medical client would get two instances with zero data crossover, by
construction, not by policy. Feneris gets its own instance for security
incidents. Same engine everywhere. Separate data, always.

## 3. Core concepts

### The node

Everything Mimir stores is a **node** — one atomic fact, rule, or reference.

| Field | Purpose |
|---|---|
| `id` | UUID |
| `layer` | `root` \| `trunk` \| `branch` \| `leaf` |
| `topic` | Stable slug identifying *what this is about* — the key that makes conflict detection possible |
| `content` | The fact itself |
| `description` | One-line summary, used for index-level recall matching |
| `tags` | Keywords for retrieval |
| `confirmations` | Times reconfirmed across sessions — drives promotion |
| `created_at` / `last_confirmed_at` / `expires_at` | `expires_at` is leaf-only |
| `authored_by` | Session/program identity string |
| `origin_machine` | Which machine wrote this node (`citadel-2.0`, `echo-station`, `avrs-trunk`, …) — cheap to carry from day one, real surgery to back-fill later |
| `hash` / `prev_hash` | Hash-chain link, **per node** — not one global chain |
| `superseded_by` | Set when a higher-layer write wins a topic collision — the loser stays in the database, just excluded from `recall()` |
| `archived_at` | Set when a leaf expires unconfirmed — same principle: excluded from `recall()`, never deleted (see Layer mechanics above) |

### Links — minimal bookkeeping, not a node field

Related-node references live in their own table (`links(from_id, to_id)`,
§4), not as a field set at write time — `add_link()` / `get_links()` /
`remove_link()` operate on already-existing nodes only, deliberately kept
independent of `write_node()` (a node write partially succeeding while a
requested link target turns out invalid isn't an ambiguity worth creating).
`get_links()` returns both directions labeled separately (`outgoing` /
`incoming`), since "this node references X" and "X references this node"
mean different things. **`recall()` does not traverse links** — this is
record-keeping, not graph expansion; making recall link-aware is a real,
separate decision if it's ever wanted (hop limits, cycles, which layers it
applies to), not something to fold in by default.

### Layer mechanics

Formalized from how the model was originally described: root as constant and
hardlined, leaf as short-term. The piece that was missing was giving leaf an
actual lifecycle — decay by default, promotion by evidence.

- **Root — hardlined.** Write requires explicit human authorization — the
  same identity + confirmation gate Bastion Gate already uses for privileged
  operations. Effectively never expires, effectively never changes.
- **Trunk — standing law.** Changeable, but every write is a deliberate,
  logged act. Touched rarely, on purpose.
- **Branch — working memory.** Normal domain knowledge. Read and write
  freely within a project's scope.
- **Leaf — short-term.** Carries an `expires_at`. Decays out of active
  recall automatically — *unless* it's reconfirmed across separate
  sessions, at which point it consolidates upward into branch. **Decay is
  never deletion** — an expired leaf is marked `archived_at` and excluded
  from `recall()`, but the row, its content, and its full `edit_log`
  history stay in the database permanently, reachable via
  `list_archived()`. Same principle as `FAILED_CLOSED` elsewhere in this
  project: sealed and dormant, not erased. Nothing Mimir stores is ever
  actually deleted, full stop — decay changes visibility, never existence.

That promotion step is the actual learning mechanism. A fact earns a
permanent home by surviving repeated contact with reality, not by being
asserted once and filed away. **Automatic promotion stops at branch,
always** — found during hardening that this needed to be an explicit rule,
not an assumption: without a cap, a completely unauthenticated node could
reach trunk through repeated confirmation alone, fully bypassing the
authorization gate `write_node()` enforces for a direct trunk write.
Elevating a branch fact to trunk requires an explicit, authorized
`write_node()` call to the same topic — which already works via the
precedence rules above — never repetition by itself.

#### Looking further out — the Aspen Model (conditional on AVRS's own growth)

If AVRS eventually runs a master-control instance governing every other AVRS
deployment, root is where that connects — many trunks, one root system, the
way an aspen grove is visually many separate trees sharing a single root
network underground. Trunk/branch/leaf stay exactly as isolated per instance
as designed above; that isolation is what keeps one client's engagement out
of another's, and nothing about this changes that. Root is already the one
layer built to be shared and centrally governed — authorization-gated
writes, rarely touched. A master AVRS administering root across every
instance it governs is an extension of that existing mechanic, not a
redesign of it. **Nothing in v1 needs to build this** — it's captured here so
the schema (already instance-tagged, already hash-chained per node) isn't
accidentally designed to foreclose it later.

### Precedence & conflict resolution

The rule — a leaf fact never overrides a trunk rule, a trunk rule never
overrides a root fact — has to be enforced by the system, not remembered by
whatever's calling it.

- Nodes that describe the same fact share a `topic` slug.
- On write, if the new node's topic collides with an existing node at a
  **different** layer: the higher layer wins automatically. The lower one
  isn't deleted — it's marked `superseded_by`, pointing at the winner. The
  conflict stays visible in the record instead of vanishing.
- Same-layer collisions are **not** auto-resolved by recency — flagged for a
  human to actually decide, every time.

## 4. Storage schema

SQLite. One file per instance. No server process to keep alive, no network
dependency for the storage layer itself.

```sql
nodes(
  id, layer, topic, content, description, tags,
  created_at, last_confirmed_at, expires_at, confirmations,
  authored_by, origin_machine, hash, prev_hash, superseded_by
)

links(from_id, to_id)

edit_log(
  id, node_id, at, actor, diff, hash
)  -- append-only, hash-chained per node

conflicts(
  -- one accumulating record per (topic, layer), not one row per colliding
  -- pair — the original node_a/node_b-pair design was O(n^2) in the number
  -- of colliding writes (measured: 6 same-layer writes to one topic -> 15
  -- rows). node_ids (JSON array) holds every node still competing. A unique
  -- partial index on (topic, layer) WHERE resolved_at IS NULL makes "at
  -- most one open conflict per collision" a database-enforced invariant.
  id, topic, layer, node_ids, detected_at, resolved_at, resolution
)
```

### Why local/single-instance doesn't mean locked-in

This isn't about pooling data between consumers — that never happens (§2,
§9). It's about one *single instance* (Feneris's, say, or one specific AVRS
engagement's) possibly needing to span more than one machine later. That
stays cheap only if three things are true from day one:

1. Node IDs are UUIDs — no two machines running the same instance ever
   collide.
2. Each node carries its own hash-chain rather than one global sequence —
   spanning machines becomes "combine independent histories," not
   "reconcile a timeline that assumed a single writer."
3. Every node is tagged with the machine that wrote it (`origin_machine`).

All three are already in the schema above. The fourth condition is a rule,
not a column: **nothing touches an instance's SQLite file directly** — only
that instance's local HTTP API. That's what lets any given instance's
storage grow into "spans machines via something avrs-bridge-shaped" later
without its consumer's integration code changing at all.

## 5. API surface

Local HTTP, plain JSON. Deliberately boring, so Kotlin, Python, and
PowerShell can all call it the same way a Mk2 addon like Skjoldr already
gets called.

```
POST /recall                     { query, context, layers? }
POST /write                       { node, layer, authorized_by?, confirmation? }
POST /confirm                     { node_id }
POST /revise                      { node_id, content?, description? }
POST /consolidate                 {}
POST /verify_chain                { node_id }
GET  /conflicts
GET  /archived
POST /resolve_conflict            { conflict_id, resolution }
POST /resolve_conflict_by_choosing { conflict_id, winner_node_id }
POST /link                        { from_id, to_id }
POST /unlink                      { from_id, to_id }
POST /links                       { node_id }
```

`write` at `root`/`trunk` requires `authorized_by` + `confirmation: true`.
Branch and leaf don't — that asymmetry is the whole point of the layer
split, enforced in code rather than left to good behavior.

## 6. Retrieval — the actual recall-lag fix

The problem was never the layer model — it was that "recall" meant loading
everything and hoping a one-line description was enough to judge relevance.

**Design rule:** root and trunk always load in full on every recall call —
they're supposed to be small and universally relevant, so there's nothing to
filter. Branch and leaf go through relevance filtering — a tag/keyword match
in v1: cheap, deterministic, no external dependency. If this subsystem is
ever unavailable, recall degrades to root+trunk only — never to nothing.

An embedding-based similarity search is a reasonable v2, but only once
there's a documented answer for what happens when the embedding backend is
down.

## 7. Provenance

Every write appends to that node's hash-chained edit log — the same
ledger/receipt pattern Mk2 already runs for its own actions, applied to
memory instead of invented a second time. Root and trunk writes additionally
carry the identity string Mk2 already requires for step approval
(`"Justin Hughes | GhostWolf101"`) — one authorization pattern, reused.

## 8. Instances, not integration

"Integration" undersells it — nobody plugs into a shared Mimir. Each
consumer stands up its **own** instance, from the same framework, with its
own database file, its own port, its own root/trunk/branch/leaf hierarchy.
What's shared is the engine's code and mechanics, never the data.

| Consumer | Its own, isolated instance |
|---|---|
| Claude Code (this AI) | Optionally, eventually, its own instance for exactly what's in its memory directory today — never shared with any other consumer's instance |
| Mk2 (Kotlin), general | Its own instance for orchestrator-level facts — called over local HTTP, same shape as any other addon |
| AVRS for Client A (financial) | Its own instance, scoped to that engagement only |
| AVRS for Client B (medical) | A **completely separate** instance — same framework, zero data overlap with Client A's |
| Feneris | Its own instance recalling security incidents — e.g. consolidating "this interface throws frequent benign `regdom_change` events" from repeated leaf-level observations into a branch-level baseline |
| Skjoldr | Its own instance, if it needs memory beyond its current snapshot files at all |
| A local LLM harness | Its own instance — this is specifically what gives a weak-recall local model persistent, structured memory it stops having to relearn every session |

Isolation isn't just tidiness — for AVRS running client-facing engagements,
keeping one client's memory instance structurally incapable of leaking into
another's is a real requirement, not a nice-to-have.

## 9. Non-goals, v1

- Not a general-purpose vector database.
- **Not a shared or central memory store, ever** — every deployment gets its
  own isolated instance. Not a v1 limitation to relax later; the design.
- Not networked across instances in v1 — cross-instance sync (below) is a
  capability a specific instance can opt into later, not something instances
  do with each other by default.
- Not a replacement for Mk2's own operational ledger — Mimir is memory and
  context, not the audit trail for actions taken.
- No persona or identity layer of any kind — memory stays tool context,
  never identity, full stop.
- **Not a general company-knowledge layer or a substitute for a system's own
  audit/ledger — settled 2026-09-04, tested against real code, not assumed.**
  Checked four real candidates in one session before writing this down:
  Feneris (already has its own restart-surviving persistence — doesn't have
  the statelessness problem this framework solves), AVRS's public demo page
  (memory here risks leaking sensitive content to random visitors — same
  shape as the standing "no capability feeds a public dashboard" rule,
  applied to memory instead), SQUAD/Pathfinder (the client-side session
  vault already does per-veteran memory correctly, client-side-only, real
  Web Crypto, already audited — porting this framework into the browser
  would re-solve a solved problem, and a server-side version would break
  the zero-server-side-disclosure law that whole rebuild exists to uphold),
  and Mk2's own `core/kintsugi/memory.ts` (real, hash-chained, genuine read
  functions — but every read feeds chain-integrity verification or policy
  reconciliation, never planning; nothing in `planner.ts`/`router.ts` reads
  it back to inform a new decision — it's memory in the Kintsugi sense,
  "the scar is part of the record," not in this framework's sense). The one
  shape that actually fits: a stateless AI consumer with the same kind of
  relationship across many otherwise-separate sessions and no persistence
  of its own — Claude Code today, or an AI's own reasoning/planning recall
  if one of Mk2's deployments ever needs it. Never the company data itself.

## 10. Open decisions

1. **Cross-instance sync — settled: no instance talks to another by
   default, ever.** If one *specific* deployment needs its own instance
   replicated across machines (e.g. an AVRS engagement monitored from both
   the GCP VM and Citadel 2.0), that's an opt-in capability of that
   instance, reusing the `avrs-bridge` pull/push pattern. Never instances
   pooling data with each other.
2. **Daemon language — settled: Python.** Already the language of Feneris
   and Skjoldr's Linux side, trivial SQLite + stdlib HTTP, easiest fit for a
   local LLM harness to call into.
3. **Repo home — settled: this repo.** A framework consumers instantiate
   their own copy of, not a service they call into.
4. **Consolidation threshold — proposed: 3 reconfirmations** before
   leaf→branch promotion, echoing the "3 boots / 5 min" convention already
   in Feneris's circuit breaker. Adjustable.

## Naming

"Mimir" is final, not a placeholder. Chosen deliberately over "Mnema" —
that name is already a distinct, separately-named aspect of AVRS, and
reusing it here would have collided with a real existing concept. Nothing
in this design ever depended on the answer either way.
