"""A human-readable view of the archive, for Finder rather than for code.

Every file on disk is named for its Plaud recording id — `077d77602e07….mp3` —
because that id is the join key for the whole pipeline and is the one name that
never changes. It is also completely opaque, so opening the archive shows sixty
identical-looking hashes and no way to find the meeting you were looking for.

This module builds `PLAUD/by-name/`, where the same files appear as

    audio/2026-08-31 1202 · 174m · Metric Health SOC2 Compliance Audit.mp3

**Symlinks, not renames.** The audio is the one artifact that cannot be
regenerated, and a title is a machine proposal that changes whenever the model is
re-run or you rewrite it by hand. Renaming would mean the precious file moves every
time a guess changes, breaking every stored path, Obsidian link and Finder alias
pointing at it. A link tree is disposable: delete it, rebuild it, and nothing is
lost. The hash tree stays canonical and this is a view over it.

**Symlinks here, copies in tiering** — the opposite of the choice `tiering.py`
makes, for the opposite reason. `stack/` is read by an indexer that may or may not
follow links, and a link followed into the full corpus is a privacy failure. Nothing
reads `by-name/`; a person does. So the risk runs the other way and links are right.

Excluded recordings never appear here, on the same rule as everywhere else.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

from .config import Config
from .store import Store

KINDS = (("audio", "audio_path"), ("transcripts", "transcript_path"), ("summaries", "summary_path"))

# Illegal on the filesystem, plus the ones that merely make a name miserable to type.
_BAD = re.compile(r'[/\\:\x00-\x1f]')


def browse_dir(cfg: Config) -> Path:
    return cfg.archive_root / "by-name"


def _stem(row) -> str:
    """`2026-08-31 1202 · 174m · Metric Health SOC2 Compliance Audit`.

    Date first so the directory sorts chronologically, which is how you look for a
    recording when you cannot remember what it was called. Duration next because it
    is the fastest way to tell a two-minute voice memo from a real conversation.
    """
    when = time.strftime("%Y-%m-%d %H%M", time.localtime(row["started_at"]))
    mins = round((row["duration_s"] or 0) / 60)
    title = (row["title"] or "").strip()
    title = _BAD.sub("-", title).strip(". ")
    parts = [when, f"{mins}m"]
    if title:
        parts.append(title[:80])
    return " · ".join(parts)


def rebuild(cfg: Config, store: Store) -> dict:
    """Reconcile PLAUD/by-name/ with the archive. Idempotent, and destroys nothing:
    only symlinks are ever removed, so a real file that somehow lands in here is left
    alone rather than deleted."""
    root = browse_dir(cfg)
    rows = store.visible()                  # tier `exclude` never reaches here

    wanted: dict[Path, Path] = {}
    used: set[Path] = set()
    for row in rows:
        stem = _stem(row)
        for kind, column in KINDS:
            raw = row[column]
            if not raw:
                continue
            src = Path(raw)
            if not src.exists():
                continue
            dst = root / kind / f"{stem}{src.suffix}"
            if dst in used:
                # Two recordings can round to the same minute and title. The id is
                # the only thing guaranteed to differ, so it breaks the tie.
                dst = root / kind / f"{stem} ({row['id'][:6]}){src.suffix}"
            used.add(dst)
            wanted[dst] = src

    for kind, _ in KINDS:
        (root / kind).mkdir(parents=True, exist_ok=True)

    linked = replaced = removed = 0
    for dst, src in wanted.items():
        # Relative, so the tree survives the volume being mounted somewhere else.
        rel = os.path.relpath(src, dst.parent)
        if dst.is_symlink():
            if os.readlink(dst) == rel:
                continue
            dst.unlink()
            replaced += 1
        elif dst.exists():
            continue        # a real file someone put here; not ours to touch
        dst.symlink_to(rel)
        linked += 1

    for kind, _ in KINDS:
        for path in (root / kind).iterdir():
            if path.is_symlink() and path not in wanted:
                path.unlink()
                removed += 1

    (root / "README.txt").write_text(
        "These are symlinks into the archive, rebuilt by `plaudctl browse`.\n"
        "The real files live under audio/, transcripts/ and summaries/, named by\n"
        "recording id. Renaming or deleting anything in here changes nothing; run\n"
        "`plaudctl browse` to put it back. Recordings tiered `exclude` never appear.\n"
    )
    return {"recordings": len(rows), "links": len(wanted),
            "created": linked, "replaced": replaced, "removed": removed}
