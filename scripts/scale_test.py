#!/usr/bin/env python3
"""Does this actually fix recall lag, or just move where the O(n) scan
happens? 12 nodes proves the mechanics. This proves — or disproves — the
actual performance claim at volume, honestly, with real numbers.
"""

import random
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir import schema, storage  # noqa: E402

WORDS = (
    "network firewall dns https certificate rotation deploy pipeline "
    "kernel driver watchdog ledger provenance hash chain conflict layer "
    "promotion decay client engagement receipt gate policy audit incident"
).split()


def make_content(rng: random.Random) -> tuple[str, str, str]:
    topic = f"synthetic.{rng.randrange(1_000_000)}"
    words = rng.sample(WORDS, k=6)
    content = " ".join(words) + f" observed at tick {rng.randrange(100000)}"
    description = " ".join(words[:3])
    return topic, content, description


def bulk_insert(conn, n: int, layer: str, rng: random.Random) -> float:
    start = time.perf_counter()
    for _ in range(n):
        topic, content, description = make_content(rng)
        storage.write_node(
            conn, layer=layer, topic=topic, content=content, description=description,
            authored_by="scale-test", origin_machine="citadel-2.0",
        )
    return time.perf_counter() - start


def timed_recall(conn, query: str, layers=None, repeats: int = 20) -> float:
    times = []
    for _ in range(repeats):
        start = time.perf_counter()
        storage.recall(conn, query=query, layers=layers)
        times.append(time.perf_counter() - start)
    return sum(times) / len(times)


def main() -> None:
    rng = random.Random(42)
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    conn = schema.connect(tmp.name)

    # A handful of root/trunk nodes, same as a real instance would have —
    # these are what recall() must ALWAYS return in full, regardless of scale.
    for i in range(5):
        storage.write_node(
            conn, layer="root" if i < 2 else "trunk", topic=f"standing.{i}",
            content=f"standing fact {i}", authored_by="scale-test", origin_machine="citadel-2.0",
            authorized_by="Justin Hughes | GhostWolf101", confirmation=True,
        )

    print(f"{'N branch/leaf nodes':>20} | {'insert (s)':>11} | {'recall, matching query (ms)':>28} | {'recall, no match (ms)':>22}")
    print("-" * 92)

    total = 0
    for batch in (100, 900, 4000, 15000):  # cumulative: 100, 1000, 5000, 20000
        insert_time = bulk_insert(conn, batch, "leaf" if batch < 5000 else "branch", rng)
        total += batch

        # A query guaranteed to match a decent chunk of what's there.
        matching_ms = timed_recall(conn, "firewall dns", repeats=10) * 1000
        # A query guaranteed to match nothing — worst case for a full scan.
        nomatch_ms = timed_recall(conn, "zzz_no_such_term_anywhere", repeats=10) * 1000

        print(f"{total:>20,} | {insert_time:>11.3f} | {matching_ms:>28.2f} | {nomatch_ms:>22.2f}")

    print(f"\nFinal row is what matters: at {total:,} branch/leaf nodes, is recall still")
    print("fast enough to call solved, or has it just moved the bottleneck?")

    Path(tmp.name).unlink()


if __name__ == "__main__":
    main()
