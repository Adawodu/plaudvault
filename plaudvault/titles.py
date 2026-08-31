"""Give a recording a name a human would recognise.

Plaud names files after the device and the clock — `2026-07-14 09:12`. That is fine
for a filesystem and useless for an inbox: thirty rows of timestamps tell you nothing
about which one was the call with the lawyer. Every other stage in this pipeline reads
the recording; this one just says what it was.

Titles are generated from the *summary* where one exists, because the summary has
already done the map-reduce over a long conversation and its Key points are a far
better title source than the first 8k characters of raw ASR. Short recordings never
get summarized (`summarize_min_seconds`), and those are exactly the voice memos whose
timestamp tells you least — so they fall back to the transcript.

The title is a proposal like everything else here. `title_source` records whether the
machine or a person wrote it, and a re-run never overwrites a human's.
"""

from __future__ import annotations

import re

from .config import Config
from .llm import available
from .store import Store
from .summarize import _generate, summary_path
from .transcribe import read_transcript

# Anything the archive would have known anyway is not a title. A model that cannot
# find a subject reaches for these, and a list of thirty "Business Discussion" rows is
# no better than thirty timestamps.
_GENERIC = frozenset(
    "conversation discussion meeting call recording audio transcript notes chat talk "
    "voice memo untitled general update catch-up catchup misc various".split()
)

PROMPT = """Name this recording.

Write a title a person would recognise in a list six months from now. Name the
specific subject: the people, the company, the decision, the problem. Prefer the
concrete noun over the category.

Rules:
- 3 to 8 words. No trailing punctuation, no quotation marks, no date, no time.
- Do not start with "Discussion of", "Conversation about", "Meeting regarding",
  "Recording of" or anything like them — go straight to the subject.
- Use names that appear in the text. Never invent a name, a company or a number.
- If the content is genuinely too thin or too garbled to name, reply exactly: UNKNOWN

Good: Clinic pilot scope with the co-founder
Good: Transmission quote from the garage
Good: Debugging the scheduler integration
Bad:  Discussion About Various Business Topics
Bad:  Meeting Recording 2026-07-14

Return ONLY the title, on one line, with no preamble.

CONTENT:
{body}

TITLE:"""

# Enough of a long recording to name it. The summary is usually well under this; the
# transcript fallback is truncated, which is fine — a title is about what a recording
# *is*, and that is established early far more often than it is not.
MAX_BODY = 6000


def _clean(raw: str) -> str:
    """Strip the model's decorations. Returns "" for anything unusable."""
    title = (raw or "").strip().splitlines()[0] if (raw or "").strip() else ""
    title = title.strip().strip("\"'“”‘’ ").strip()
    title = re.sub(r"^(?:title|the title(?: is)?)\s*[:\-—]\s*", "", title, flags=re.I)
    title = re.sub(r"^\**|\**$", "", title).strip()
    # A model that ignored "no preamble" sometimes emits a whole sentence.
    title = title.rstrip(".").strip()
    if not title or title.upper() == "UNKNOWN":
        return ""
    if len(title) > 120 or len(title.split()) > 14:
        return ""
    words = [w for w in re.findall(r"[a-z]+", title.lower())]
    if words and all(w in _GENERIC for w in words):
        return ""
    return title[:200]


def title_for(cfg: Config, *, summary: str = "", transcript: str = "") -> str:
    """Propose a title from whatever text there is. "" means "could not name it"."""
    body = (summary or transcript or "").strip()
    if len(body) < 80:
        return ""
    return _clean(_generate(cfg, PROMPT.format(body=body[:MAX_BODY]), timeout=180))


def run(cfg: Config, store: Store, *, limit: int | None = None, force: bool = False) -> dict:
    ok, why = available(cfg)
    if not ok:
        raise RuntimeError(f"language model unavailable — {why}")

    if force:
        # `--force` re-titles the machine's own work, never yours. A hand-written
        # title is a decision, and a decision does not get overwritten by a re-run —
        # the same rule triage lives by.
        rows = [
            r for r in store.visible()
            if r["transcript_path"] and r["title_source"] != "human"
        ]  # includes ones previously declined — a better summary may now name them
    else:
        rows = store.needing_title()
    if limit:
        rows = rows[:limit]

    stats = {"titled": 0, "unnamed": 0, "failed": 0}
    print(f"  {len(rows)} to title · {cfg.llm_label}")

    for i, row in enumerate(rows, 1):
        sp = summary_path(cfg, row["id"])
        summary = sp.read_text() if sp.exists() else ""
        transcript = "" if summary else read_transcript(cfg, row["id"])
        try:
            title = title_for(cfg, summary=summary, transcript=transcript)
            if not title:
                # Record the visit so the next run does not pay for the same call
                # again. A recording nothing can name is a permanent state, not a
                # queue item.
                store.mark_title_attempted(row["id"])
                stats["unnamed"] += 1
                print(f"  [{i}/{len(rows)}] {row['filename'][:50]} — not nameable, left as is")
                continue
            store.set_title(row["id"], title, source="model")
            stats["titled"] += 1
            print(f"  [{i}/{len(rows)}] {title}")
        except Exception as exc:  # noqa: BLE001
            stats["failed"] += 1
            print(f"  [{i}/{len(rows)}] [fail] {exc}")

    return stats


def display(row) -> str:
    """What to show for a recording, everywhere. One rule, one place."""
    try:
        title = row["title"]
    except (KeyError, IndexError):
        title = None
    return title or row["filename"]


def retitle_after(cfg: Config, store: Store, rec_id: str) -> str:
    """Re-title one recording now — used after a summary or the speakers change.

    Naming a speaker turns "SPEAKER_01 said they would send the contract" into
    "Herry said they would send the contract", which is frequently the difference
    between a nameable recording and an unnameable one.
    """
    row = store.get(rec_id)
    if row is None or row["title_source"] == "human":
        return row["title"] if row else ""
    sp = summary_path(cfg, rec_id)
    title = title_for(
        cfg,
        summary=sp.read_text() if sp.exists() else "",
        transcript="" if sp.exists() else read_transcript(cfg, rec_id),
    )
    if title:
        store.set_title(rec_id, title, source="model")
    return title
