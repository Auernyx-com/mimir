#!/usr/bin/env python3
"""Sync a specific list of just-edited markdown memory files into the real
Mimir instance via revise_node — not write_node, since these are revisions
to existing topics, not new competing claims."""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir import schema, storage  # noqa: E402

MEMORY_DIR = Path.home() / ".claude/projects/-home-justin/memory"
INSTANCE_DB = Path.home() / ".claude/mimir/claude-code.db"
FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)

TOPICS = [
    "feneris-family",
    "standing-hard-stops-and-vocabulary",
    "auernyx-architecture-detail",
    "wyerd-revenue-products",
    "squad-bat-divisions-laws",
    "working-style-protocol",
    "security-incident-context",
    "echostation-forge-memory",
]


def parse(path: Path) -> dict:
    text = path.read_text()
    m = FRONTMATTER_RE.match(text)
    fm, body = m.groups()
    desc = re.search(r'^description:\s*"?(.+?)"?$', fm, re.MULTILINE)
    return {"description": desc.group(1).strip() if desc else "", "content": body.strip()}


def main() -> None:
    conn = schema.connect(INSTANCE_DB)
    for topic in TOPICS:
        path = MEMORY_DIR / f"{topic}.md"
        parsed = parse(path)
        row = conn.execute(
            "SELECT id FROM nodes WHERE topic = ? AND superseded_by IS NULL", (topic,)
        ).fetchone()
        if row is None:
            print(f"  SKIP (no existing node): {topic}")
            continue
        storage.revise_node(
            conn, row["id"], content=parsed["content"], description=parsed["description"],
            actor="claude-code-forge-integration",
        )
        print(f"  revised: {topic}")
    conn.close()


if __name__ == "__main__":
    main()
