"""Pull commitments out of a transcript, locally, as *proposals*.

Everything this produces lands in status `proposed` and is worthless until a human
accepts it in the console. That's deliberate: a local model reading noisy ASR of a
family conversation will confidently invent obligations, so extraction is a
suggestion engine, not an authority. Precision is favoured over recall — a missed
action costs you a scroll through the transcript; a fabricated one costs trust in
the whole board.
"""

from __future__ import annotations

import json
import re
import time

from .config import Config
from .store import Store
from .llm import available
from .summarize import _chunk, _generate
from .transcribe import read_transcript

EXTRACT_PROMPT = """Read this conversation transcript and list the things that should end up
on a to-do list.

Two kinds count:
- "commitment" — someone said they would do it ("I'll send it Friday", "I need to email Herry")
- "suggestion" — the discussion clearly implies a useful next step, even if nobody
  explicitly committed to it

Return a JSON array. Each element:
{{"text": "the action, imperative, one line",
  "kind": "commitment" or "suggestion",
  "owner": "who would do it, or empty string if unclear",
  "quote": "the transcript line it came from",
  "at": "the [HH:MM:SS] timestamp of that line"}}

Here is a worked example.

TRANSCRIPT:
[00:01:10] Okay, on the billing work, I'll draft the vendor agreement by Friday and send it over.
[00:01:32] And I need to email Dana to set up the review call.
[00:02:05] Yeah, the weather has been strange lately.
[00:02:40] The renewal dates are scattered across three spreadsheets, it's a mess.

OUTPUT:
[{{"text":"Draft the vendor agreement and send it over","kind":"commitment","owner":"Speaker 1","quote":"I'll draft the vendor agreement by Friday and send it over","at":"00:01:10"}},
 {{"text":"Email Dana to set up the review call","kind":"commitment","owner":"Speaker 1","quote":"And I need to email Dana to set up the review call","at":"00:01:32"}},
 {{"text":"Consolidate the renewal dates into one source","kind":"suggestion","owner":"","quote":"The renewal dates are scattered across three spreadsheets","at":"00:02:40"}}]

Note what was skipped: the weather remark is small talk, so it produced nothing.

Guidance: skip small talk and pleasantries. This transcript comes from automatic speech
recognition, so if a line is too garbled to understand, skip it rather than guessing at
it. Plenty of conversations contain nothing actionable — if this is one of them, return
an empty array [] rather than manufacturing something.

Return ONLY the JSON array, no prose and no code fences.

TRANSCRIPT:
{chunk}

OUTPUT:
"""

# Brackets optional: the model echoes "[00:01:10]" from the transcript in `quote`,
# but returns a bare "00:01:10" in `at`.
TS_RE = re.compile(r"\[?(\d{1,2}):(\d{2}):(\d{2})\]?")


def _parse_ts(text: str) -> int | None:
    m = TS_RE.search(text or "")
    if not m:
        return None
    h, mnt, s = (int(g) for g in m.groups())
    return (h * 3600 + mnt * 60 + s) * 1000


def _parse_json_array(raw: str) -> list[dict]:
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
    start, end = raw.find("["), raw.rfind("]")
    if start < 0 or end < 0:
        return []
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return []
    return [d for d in data if isinstance(d, dict) and (d.get("text") or "").strip()]


# Words too common to prove anything about where a quote came from.
_STOP = frozenset(
    "the a an and or of to in for on is are was were be been i you he she it we they "
    "that this with as at by from need needs going gonna so like just have has had do "
    "does did will would can could my your our their".split()
)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", (text or "").lower())).strip()


def _grounded(quote: str, chunk_norm: str) -> bool:
    """Can this quote be traced back to the text the model was actually given?

    A few-shot example is the one thing in the prompt that looks exactly like a
    correct answer, and a small model will occasionally return it instead of reading
    the transcript — producing an action with a citation to a conversation that never
    happened. That is worse than a wrong action: the quote is the thing you would
    check it against, so a fabricated quote defeats its own audit.

    Verbatim matching is too strict, because models legitimately elide and reword
    ("...", "[have them]"), and on real data that would have discarded 23 sound
    actions to catch 2 bad ones. So a quote passes if a 40-character run of it appears
    verbatim, or if most of its content words do. Both leaked examples fail; every
    genuine paraphrase in the corpus passes.
    """
    q = _norm(quote)
    if len(q) < 12:
        return True  # nothing to verify against; the text itself is judged elsewhere
    if q in chunk_norm:
        return True
    if any(q[i : i + 40] in chunk_norm for i in range(0, max(1, len(q) - 40), 10)):
        return True
    words = [w for w in q.split() if w not in _STOP and len(w) > 3]
    if not words:
        return True
    return sum(w in chunk_norm for w in words) / len(words) >= 0.6


def extract_from_text(cfg: Config, text: str) -> list[dict]:
    found: list[dict] = []
    dropped = 0
    for chunk in _chunk(text):
        raw = _generate(cfg, EXTRACT_PROMPT.format(chunk=chunk))
        chunk_norm = _norm(chunk)
        for item in _parse_json_array(raw):
            if not _grounded(str(item.get("quote") or ""), chunk_norm):
                dropped += 1
                continue
            kind = str(item.get("kind") or "commitment").strip().lower()
            if kind not in ("commitment", "suggestion"):
                kind = "commitment"
            found.append(
                {
                    "text": str(item["text"]).strip()[:500],
                    "kind": kind,
                    "owner": str(item.get("owner") or "").strip()[:100],
                    "quote": str(item.get("quote") or "").strip()[:500],
                    "at_ms": _parse_ts(str(item.get("at") or "")) or _parse_ts(str(item.get("quote") or "")),
                }
            )

    # Near-duplicate collapse across chunk boundaries.
    seen: set[str] = set()
    unique = []
    for f in found:
        key = re.sub(r"[^a-z0-9 ]", "", f["text"].lower())[:60]
        if key in seen:
            continue
        seen.add(key)
        unique.append(f)
    if dropped:
        # Never silent: a filter you can't see is one you stop trusting.
        print(f"    [dropped {dropped} with a quote not traceable to the transcript]")
    return unique


def run(cfg: Config, store: Store, *, limit: int | None = None, force: bool = False) -> dict:
    rows = store.all() if force else [
        r for r in store.all() if r["transcript_path"] and not r["extracted_at"]
    ]
    rows = [r for r in rows if r["transcript_path"]]
    if limit:
        rows = rows[:limit]

    stats = {"recordings": 0, "proposed": 0, "failed": 0}
    ok, why = available(cfg)
    if not ok:
        raise RuntimeError(f"language model unavailable — {why}")
    print(f"  {len(rows)} recordings to scan for commitments · {cfg.llm_label}")

    for i, row in enumerate(rows, 1):
        text = read_transcript(cfg, row["id"])
        if not text.strip():
            continue
        print(f"  [{i}/{len(rows)}] {row['filename'][:60]} ...", flush=True)
        try:
            existing = {
                re.sub(r"[^a-z0-9 ]", "", a["text"].lower())[:60]
                for a in store.actions(recording_id=row["id"])
            }
            n = 0
            for item in extract_from_text(cfg, text):
                key = re.sub(r"[^a-z0-9 ]", "", item["text"].lower())[:60]
                if key in existing:
                    continue
                store.add_action(recording_id=row["id"], **item)
                n += 1
            store.update(row["id"], extracted_at=int(time.time()))
            stats["recordings"] += 1
            stats["proposed"] += n
            print(f"    {n} proposed")
        except Exception as exc:  # noqa: BLE001
            stats["failed"] += 1
            print(f"    [fail] {exc}")

    return stats
