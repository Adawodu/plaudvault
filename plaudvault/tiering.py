"""Make the triage decision physically enforce what reaches the cognitive stack.

A tier stored only in a database is a promise. This module turns it into a fact:
`PLAUD/stack/` contains a copy of exactly those transcripts tiered `stack`, and the
catalog policy points at that directory rather than at `transcripts/`. Untier a
recording and its copy is removed on the next sync, so it leaves the corpus.

Copies, not symlinks — indexers vary in whether they follow links, and "the catalog
silently followed a link into the full corpus" is exactly the failure this prevents.
"""

from __future__ import annotations

from pathlib import Path

from .config import Config
from .store import Store


def stack_dir(cfg: Config) -> Path:
    return cfg.archive_root / "stack"


def sync(cfg: Config, store: Store) -> dict:
    """Reconcile PLAUD/stack/ with the current `stack` tier. Idempotent."""
    target = stack_dir(cfg)
    target.mkdir(parents=True, exist_ok=True)

    approved = {}
    for row in store.by_tier("stack"):
        if not row["transcript_path"]:
            continue
        src = Path(row["transcript_path"])
        if src.exists():
            approved[f"{row['id']}.txt"] = src

    present = {p.name: p for p in target.glob("*.txt")}

    added = removed = updated = 0
    for name, src in approved.items():
        dst = target / name
        body = src.read_text()
        if not dst.exists():
            dst.write_text(body)
            added += 1
        elif dst.read_text() != body:
            dst.write_text(body)
            updated += 1

    for name, path in present.items():
        if name not in approved:
            path.unlink()
            removed += 1

    return {"approved": len(approved), "added": added, "updated": updated, "removed": removed}
