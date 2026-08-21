"""Local summarization via Ollama. Transcript text never leaves 127.0.0.1.

Long recordings are chunked and reduced rather than truncated — an 84-minute
conversation summarized from its first 8k tokens is worse than no summary.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .config import Config
from .llm import LLMError, available, generate
from .store import Store
from .transcribe import read_transcript

# Rough chars-per-chunk. qwen3.5 handles far more, but smaller chunks summarize
# more faithfully than one giant context stuffed to the limit.
CHUNK_CHARS = 12000

MAP_PROMPT = """You are summarizing one segment of a longer audio recording.

Write a factual digest of this segment. Capture: what was discussed, decisions made,
commitments or action items, names, numbers, and dates. Do not editorialize, do not
add a preamble, and do not invent anything not present in the text.

SEGMENT:
{chunk}
"""

REDUCE_PROMPT = """You are writing the final summary of an audio recording titled "{title}",
recorded {when}, lasting {minutes:.0f} minutes.

Below are digests of consecutive segments. Merge them into one coherent summary using
exactly this structure and these headings:

## Summary
Two to four sentences on what this recording is and what happened.

## Key points
Bullet points of the substantive content.

## Decisions
Bullet points of anything decided. Write "None recorded." if there were none.

## Action items
Bullet points, each starting with the responsible person if identifiable. Write
"None recorded." if there were none.

## Open questions
Anything raised but unresolved. Write "None recorded." if there were none.

## Tags
A single line of 3-6 lowercase hashtag-free keywords, comma separated.

Do not invent content. If the recording is casual conversation with no decisions or
actions, say so plainly rather than manufacturing items.

SEGMENT DIGESTS:
{digests}
"""


def summary_path(cfg: Config, rec_id: str) -> Path:
    return cfg.summary_dir / f"{rec_id}.md"


def _chunk(text: str, size: int = CHUNK_CHARS) -> list[str]:
    lines = text.splitlines()
    chunks, cur, cur_len = [], [], 0
    for ln in lines:
        if cur_len + len(ln) > size and cur:
            chunks.append("\n".join(cur))
            cur, cur_len = [], 0
        cur.append(ln)
        cur_len += len(ln) + 1
    if cur:
        chunks.append("\n".join(cur))
    return chunks


def _generate(cfg: Config, prompt: str, *, timeout: float = 900) -> str:
    """Kept as the single call site the prompt modules share."""
    return generate(cfg, prompt, timeout=timeout)


def summarize_text(cfg: Config, text: str, *, title: str, when: str, minutes: float) -> str:
    chunks = _chunk(text)
    if len(chunks) == 1:
        digests = chunks[0]
    else:
        digests = "\n\n".join(
            f"--- segment {i} of {len(chunks)} ---\n{_generate(cfg, MAP_PROMPT.format(chunk=c))}"
            for i, c in enumerate(chunks, 1)
        )
    return _generate(
        cfg,
        REDUCE_PROMPT.format(title=title, when=when, minutes=minutes, digests=digests),
    )


def ollama_available(cfg: Config) -> bool:
    """Back-compat alias; provider-agnostic now."""
    return available(cfg)[0]


def run(cfg: Config, store: Store, *, limit: int | None = None, force: bool = False) -> dict:
    cfg.ensure_dirs()
    ok, why = available(cfg)
    if not ok:
        raise RuntimeError(f"language model unavailable — {why}")

    rows = store.all() if force else store.needing("summary_path", requires="transcript_path")
    rows = [r for r in rows if r["duration_s"] >= cfg.summarize_min_seconds]
    if limit:
        rows = rows[:limit]

    stats = {"done": 0, "failed": 0}
    print(f"  {len(rows)} to summarize with {cfg.llm_label}")

    for i, row in enumerate(rows, 1):
        text = read_transcript(cfg, row["id"])
        if not text.strip():
            continue
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(row["started_at"]))
        print(f"  [{i}/{len(rows)}] {row['filename'][:60]} ...", flush=True)
        t0 = time.time()
        try:
            md = summarize_text(
                cfg, text, title=row["filename"], when=when, minutes=row["duration_s"] / 60
            )
            path = summary_path(cfg, row["id"])
            path.write_text(md + "\n")
            store.update(
                row["id"],
                summary_path=str(path),
                summarized_at=int(time.time()),
                summary_model=cfg.llm_label,
            )
            stats["done"] += 1
            print(f"    done in {time.time() - t0:.0f}s")
        except Exception as exc:  # noqa: BLE001
            stats["failed"] += 1
            print(f"    [fail] {exc}")

    return stats


def extract_tags(summary_md: str) -> list[str]:
    m = re.search(r"^##\s*Tags\s*$(.+?)(?=^##|\Z)", summary_md, re.M | re.S)
    if not m:
        return []
    raw = m.group(1).strip().lstrip("-* ").replace("#", "")
    parts = [p.strip().lower() for p in re.split(r"[,\n]", raw) if p.strip()]
    return [re.sub(r"[^a-z0-9-]+", "-", p).strip("-") for p in parts if len(p) < 40][:6]
