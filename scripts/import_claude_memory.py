#!/usr/bin/env python3
"""One-off validation script: import Claude Code's own memory files into a
throwaway Mimir instance, then run some real recall queries against them.

This is a TEST of the framework using real content — it does not touch, move,
or replace the live memory directory it reads from, and the resulting
instance stays local/gitignored. See SPEC.md §2: this instance would remain
Claude Code's own, isolated, if it were ever kept for real — never shared
with any other consumer's instance.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir import schema, storage  # noqa: E402

MEMORY_DIR = Path.home() / ".claude/projects/-home-justin/memory"
INSTANCE_DB = Path(__file__).resolve().parent.parent / "instances" / "claude-code-test.db"

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)


def parse_memory_file(path: Path) -> dict:
    text = path.read_text()
    m = FRONTMATTER_RE.match(text)
    if not m:
        raise ValueError(f"{path} has no frontmatter")
    fm_text, body = m.groups()

    name = re.search(r"^name:\s*(.+)$", fm_text, re.MULTILINE)
    desc = re.search(r'^description:\s*"?(.+?)"?$', fm_text, re.MULTILINE)
    layer = re.search(r"^\s*layer:\s*(\w+)$", fm_text, re.MULTILINE)

    return {
        "topic": name.group(1).strip() if name else path.stem,
        "description": desc.group(1).strip() if desc else "",
        "layer": layer.group(1).strip() if layer else "branch",
        "content": body.strip(),
    }


def main() -> None:
    INSTANCE_DB.parent.mkdir(exist_ok=True)
    INSTANCE_DB.unlink(missing_ok=True)  # fresh instance every run
    conn = schema.connect(INSTANCE_DB)

    files = sorted(MEMORY_DIR.glob("*.md"))
    files = [f for f in files if f.name != "MEMORY.md"]

    imported = []
    for f in files:
        parsed = parse_memory_file(f)
        node = storage.write_node(
            conn,
            layer=parsed["layer"],
            topic=parsed["topic"],
            content=parsed["content"],
            description=parsed["description"],
            authored_by="claude-code-migration-test",
            origin_machine="citadel-2.0",
            authorized_by="Justin Hughes | GhostWolf101",
            confirmation=True,  # importing already-trusted, already-existing memory
        )
        imported.append((parsed["layer"], parsed["topic"], node.id))
        print(f"  [{parsed['layer']:6}] {parsed['topic']}")

    print(f"\nImported {len(imported)} nodes into {INSTANCE_DB}\n")

    print("=" * 60)
    print("VALIDATION: recall with no query — should return ONLY root+trunk in full")
    print("=" * 60)
    results = storage.recall(conn, query="")
    for n in results:
        print(f"  [{n.layer:6}] {n.topic}")
    expected_root_trunk = {t for l, t, _ in imported if l in ("root", "trunk")}
    got = {n.topic for n in results}
    print(f"\nExpected root+trunk topics: {sorted(expected_root_trunk)}")
    print(f"Got:                        {sorted(got)}")
    print("MATCH" if got == expected_root_trunk else "MISMATCH")

    print("\n" + "=" * 60)
    print("VALIDATION: recall('nvidia driver') — should surface the hard-stop")
    print("=" * 60)
    results = storage.recall(conn, query="nvidia driver")
    for n in results:
        print(f"  [{n.layer:6}] {n.topic} — {n.description[:70]}")

    print("\n" + "=" * 60)
    print("VALIDATION: recall('skjoldr fortress') — should surface Feneris family + hard stops")
    print("=" * 60)
    results = storage.recall(conn, query="skjoldr fortress")
    for n in results:
        print(f"  [{n.layer:6}] {n.topic} — {n.description[:70]}")

    print("\n" + "=" * 60)
    print("VALIDATION: consolidate() on freshly-imported data — nothing should move")
    print("=" * 60)
    result = storage.consolidate(conn)
    print(f"  pruned:   {result['pruned']}")
    print(f"  promoted: {result['promoted']}")
    print("PASS — nothing pruned/promoted on fresh import" if not result["pruned"] and not result["promoted"] else "UNEXPECTED CHANGE")

    conn.close()


if __name__ == "__main__":
    main()
