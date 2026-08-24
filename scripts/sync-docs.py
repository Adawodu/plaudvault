#!/usr/bin/env python3
"""Regenerate the generated blocks of docs/PRODUCT-BIBLE.md.

Only text between `<!-- BEGIN:X -->` and `<!-- END:X -->` is ever rewritten, so the
hand-written sections — decisions, backlog, roadmap, known limits — cannot be clobbered
by a script that has no way of knowing what they should say.

Deliberately dependency-free and deterministic: it reads git and, if the archive happens
to be mounted, the manifest. A missing archive degrades to "not available" rather than
failing, because this runs from a git hook that must never block a commit.

    python scripts/sync-docs.py            rewrite the blocks
    python scripts/sync-docs.py --check    exit 1 if they are stale
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BIBLE = ROOT / "docs" / "PRODUCT-BIBLE.md"


def sh(*args: str) -> str:
    try:
        return subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=30).stdout.strip()
    except Exception:  # noqa: BLE001 — a hook must never fail the commit
        return ""


def archive_root() -> Path | None:
    """Where the live archive is, per config — or None if we cannot tell."""
    import os

    cfg = Path(os.environ.get("PLAUDVAULT_CONFIG", Path.home() / ".config/plaudvault/config.toml"))
    if not cfg.exists():
        return None
    try:
        with cfg.open("rb") as fh:
            root = Path(tomllib.load(fh).get("archive_root", "")).expanduser()
    except Exception:  # noqa: BLE001
        return None
    return root if root.is_dir() else None


# --------------------------------------------------------------------------- blocks


def block_status() -> str:
    lines = [
        f"_Generated {date.today().isoformat()} from git and the live archive._",
        "",
        "### Codebase",
        "",
        "| | |",
        "|---|---|",
    ]
    mods = sorted(
        ((p.name, len(p.read_text().splitlines())) for p in (ROOT / "plaudvault").glob("*.py")),
        key=lambda kv: -kv[1],
    )
    lines += [
        f"| Python modules | {len(mods)} |",
        f"| Lines of Python | {sum(n for _, n in mods):,} |",
        f"| Commits | {sh('git', 'rev-list', '--count', 'HEAD') or '?'} |",
        f"| CLI verbs | {len(cli_verbs())} — `{'`, `'.join(cli_verbs())}` |",
        "",
        "Largest modules: " + ", ".join(f"`{n}` ({c})" for n, c in mods[:6]) + ".",
        "",
    ]

    root = archive_root()
    lines += ["### Live archive", ""]
    if root is None:
        lines += ["_Archive not mounted or not configured — corpus figures unavailable._", ""]
        return "\n".join(lines)

    import sqlite3

    db = root / "manifest.sqlite"
    if not db.exists():
        lines += ["_No manifest at the configured archive root._", ""]
        return "\n".join(lines)
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        q = lambda s: con.execute(s).fetchone()[0]  # noqa: E731
        rows = [
            ("recordings", q("SELECT COUNT(*) FROM recordings")),
            ("transcribed", q("SELECT COUNT(*) FROM recordings WHERE transcript_path IS NOT NULL")),
            ("tone scored", q("SELECT COUNT(*) FROM sentiment")),
            ("indexed chunks", q("SELECT COUNT(*) FROM chunks")),
            ("triaged", q("SELECT COUNT(*) FROM triage")),
            ("open commitments", q("SELECT COUNT(*) FROM actions WHERE status='proposed'")),
            ("action events", q("SELECT COUNT(*) FROM action_events")),
        ]
        hours = q("SELECT COALESCE(SUM(duration_s),0) FROM recordings") / 3600
        tiers = dict(con.execute("SELECT tier, COUNT(*) FROM triage GROUP BY tier").fetchall())
        con.close()
    except Exception as exc:  # noqa: BLE001
        lines += [f"_Could not read the manifest: {exc}_", ""]
        return "\n".join(lines)

    lines += ["| | |", "|---|---|"]
    lines += [f"| {k.capitalize()} | {v:,} |" for k, v in rows]
    lines += [f"| Audio captured | {hours:.1f} hours |"]
    if tiers:
        lines += ["| Tiers | " + " · ".join(f"{k} {v}" for k, v in sorted(tiers.items())) + " |"]
    lines += [""]
    return "\n".join(lines)


def cli_verbs() -> list[str]:
    src = (ROOT / "plaudvault" / "cli.py").read_text()
    # commands come from two places: direct add("x", ...) calls, and a shared loop
    # over (name, fn, help) tuples. Miss the second and the count silently under-reports.
    verbs = re.findall(r'(?:^|\s)(?:sp = )?add\("([a-z]+)"', src)
    verbs += re.findall(r'^\s*\("([a-z]+)", cmd_\w+,', src, re.M)
    # de-duplicate while keeping declaration order
    seen, out = set(), []
    for v in verbs:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def block_shipped() -> str:
    """Every commit, newest first — the honest record of what actually landed."""
    log = sh("git", "log", "--pretty=format:%h\x1f%ad\x1f%s", "--date=short")
    if not log:
        return "_No commits yet._"
    lines = [
        "Newest first. Each commit message carries the reasoning; this is only the index.",
        "",
        "| Date | Commit | What landed |",
        "|---|---|---|",
    ]
    for row in log.splitlines():
        parts = row.split("\x1f")
        if len(parts) != 3:
            continue
        sha, when, subject = parts
        lines.append(f"| {when} | `{sha}` | {subject.replace('|', '\\|')} |")
    return "\n".join(lines)


BLOCKS = {"STATUS": block_status, "SHIPPED": block_shipped}


# ---------------------------------------------------------------------------- main


def render(text: str) -> str:
    for name, fn in BLOCKS.items():
        pattern = re.compile(
            rf"(<!-- BEGIN:{name}[^>]*-->)(.*?)(<!-- END:{name} -->)", re.S
        )
        if not pattern.search(text):
            print(f"  [warn] no {name} block in the bible — skipped", file=sys.stderr)
            continue
        body = fn()
        text = pattern.sub(lambda m: f"{m.group(1)}\n{body}\n{m.group(3)}", text)
    return text


def main() -> int:
    if not BIBLE.exists():
        print(f"error: {BIBLE} not found", file=sys.stderr)
        return 1
    current = BIBLE.read_text()
    updated = render(current)

    if "--check" in sys.argv:
        if current != updated:
            print("PRODUCT-BIBLE.md is stale — run: python scripts/sync-docs.py")
            return 1
        print("PRODUCT-BIBLE.md is current")
        return 0

    if current == updated:
        print("PRODUCT-BIBLE.md already current")
        return 0
    BIBLE.write_text(updated)
    print(f"PRODUCT-BIBLE.md updated ({', '.join(BLOCKS)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
