"""Write one Obsidian note per recording.

Notes hold the summary and link out to the transcript and audio on the archive drive
rather than inlining them — a 90-minute transcript inside a note makes the vault
unsearchable and blows up the graph. Notes are regenerated idempotently; anything
below the MANUAL marker is preserved across rewrites.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from .config import Config
from .store import Store
from .summarize import extract_tags, summary_path
from .transcribe import transcript_paths

MANUAL_MARKER = "<!-- plaudvault:manual — anything below this line is preserved -->"


def slugify(text: str, maxlen: int = 60) -> str:
    text = re.sub(r"^\d{2}-\d{2}\s+", "", text.strip())  # Plaud prefixes "08-18 "
    slug = re.sub(r"[^\w\s-]", "", text).strip()
    slug = re.sub(r"\s+", " ", slug)
    return slug[:maxlen].strip() or "recording"


def note_path(cfg: Config, row) -> Path:
    date = time.strftime("%Y-%m-%d", time.localtime(row["started_at"]))
    return cfg.notes_dir / f"{date} {slugify(row['filename'])}.md"


def _preserved(path: Path) -> str:
    if not path.exists():
        return ""
    body = path.read_text()
    idx = body.find(MANUAL_MARKER)
    return body[idx + len(MANUAL_MARKER) :].strip() if idx >= 0 else ""


def _sentiment_lines(sent) -> list[str]:
    """Frontmatter fields for the tone reading, so Dataview can query on it.

    `sentiment_confidence` ships alongside the score deliberately: a valence with no
    confidence beside it invites the vault to treat a guess as a measurement.
    """
    if sent is None:
        return []
    return [
        f"sentiment: {sent['label']}",
        f"sentiment_valence: {sent['valence']:+.2f}",
        f"sentiment_energy: {sent['energy']:.2f}" if sent["energy"] is not None else "",
        f"sentiment_confidence: {sent['confidence']:.2f}"
        if sent["confidence"] is not None
        else "",
    ]


def render(cfg: Config, row, summary_md: str, tags: list[str], sent=None) -> str:
    started = time.localtime(row["started_at"])
    date = time.strftime("%Y-%m-%d", started)
    when = time.strftime("%Y-%m-%d %H:%M", started)
    minutes = row["duration_s"] / 60

    _, txt_path = transcript_paths(cfg, row["id"])
    tag_lines = "\n".join(f"  - {t}" for t in (["plaud", "recording"] + tags))

    front = [
        "---",
        f'title: "{row["filename"].replace(chr(34), "")}"',
        f"date: {date}",
        f"recorded: {when}",
        f"duration_min: {minutes:.1f}",
        f"plaud_id: {row['id']}",
        f"device: {row['serial_number'] or 'unknown'}",
        f"transcribed_with: {row['transcribe_model'] or ''}",
        f"summarized_with: {row['summary_model'] or ''}",
        *[ln for ln in _sentiment_lines(sent) if ln],
        "source: plaud",
        "tags:",
        tag_lines,
        "---",
        "",
    ]

    tone = ""
    if sent is not None:
        tone = (
            f"\n> **Tone:** {sent['label']} · valence {sent['valence']:+.2f}"
            f" · confidence {sent['confidence']:.2f} — an estimate from the transcript,"
            " which carries no tone of voice.\n"
        )
        drivers = [d for d in (sent["drivers"] or "").split("\n") if d]
        if drivers:
            tone += "> Read from: " + "; ".join(drivers[:3]) + "\n"

    body = [
        f"# {row['filename']}",
        "",
        f"*{when} · {minutes:.0f} min · transcribed and summarized locally*",
        tone,
        summary_md.strip() if summary_md.strip() else "*(no summary — recording too short)*",
        "",
        "## Files",
        "",
        f"- Transcript: `{txt_path}`",
        f"- Audio: `{row['audio_path']}`",
        "",
        MANUAL_MARKER,
        "",
    ]
    return "\n".join(front + body)


def run(cfg: Config, store: Store, *, limit: int | None = None, force: bool = False) -> dict:
    cfg.ensure_dirs()
    if cfg.notes_dir is None:
        print("  no vault configured (vault_root is empty) — skipping notes")
        return {"written": 0, "failed": 0, "skipped": True}
    if force:
        rows = store.all()
    else:
        # Notes that don't exist yet, plus notes that have fallen behind what the
        # manifest knows — a tone reading scored after the note was written, or a
        # note file that has since been deleted from the vault. Without the second
        # set, `note_path` being non-null would permanently freeze a stale note.
        rows = store.needing("note_path", requires="transcript_path") + store.stale_notes()
    if limit:
        rows = rows[:limit]

    stats = {"written": 0, "failed": 0}
    print(f"  {len(rows)} notes to write into {cfg.notes_dir}")

    for row in rows:
        try:
            sp = summary_path(cfg, row["id"])
            summary_md = sp.read_text() if sp.exists() else ""
            tags = extract_tags(summary_md)
            sent = store.sentiment_of(row["id"])
            # drop the Tags section from the visible body — it lives in frontmatter
            visible = re.sub(r"^##\s*Tags\s*$.*", "", summary_md, flags=re.M | re.S).strip()

            path = note_path(cfg, row)
            keep = _preserved(path)
            content = render(cfg, row, visible, tags, sent)
            if keep:
                content += "\n" + keep + "\n"
            path.write_text(content)

            store.update(row["id"], note_path=str(path), noted_at=int(time.time()))
            stats["written"] += 1
        except Exception as exc:  # noqa: BLE001
            stats["failed"] += 1
            print(f"  [fail] {row['filename'][:50]}: {exc}")

    return stats
