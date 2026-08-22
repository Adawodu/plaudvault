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

# Two prompts rather than one prompt plus a filter. Asking for suggestions and then
# discarding them still spends the model's attention on inventing them, and a category
# that is merely *mentioned* is a category the model will populate. When suggestions are
# off, the concept does not appear anywhere in the prompt — not in the rules, not in the
# schema, not in the worked example.
_RULES_COMMITMENTS_ONLY = """One thing counts, and only one:
- "commitment" — a person actually said they would do it ("I'll send it Friday",
  "I need to email Herry", "let me sort that out tomorrow")

This is a strict test and most of a conversation fails it. Do NOT list something
because it sounds useful, because it was discussed at length, because it is a problem
worth solving, or because the conversation implies it would be a good next step. If
nobody said they would do it, it does not go on the list. Topics are not commitments.
Ideas are not commitments. Things that already happened during the conversation are not
commitments."""

_RULES_WITH_SUGGESTIONS = """Two kinds count:
- "commitment" — someone said they would do it ("I'll send it Friday", "I need to email Herry")
- "suggestion" — the discussion clearly implies a useful next step, even if nobody
  explicitly committed to it"""

_KIND_FIELD_COMMITMENTS_ONLY = '"kind": "commitment",'
_KIND_FIELD_WITH_SUGGESTIONS = '"kind": "commitment" or "suggestion",'

_EXAMPLE_COMMITMENTS_ONLY = """[{{"text":"Draft the vendor agreement and send it over","kind":"commitment","owner":"Speaker 1","quote":"I'll draft the vendor agreement by Friday and send it over","at":"00:01:10"}}]

Note what was skipped. The weather remark is small talk. The scattered renewal dates are
a real problem and consolidating them would obviously help — but nobody said they would
do it, so it is not a commitment and does not appear."""

_EXAMPLE_WITH_SUGGESTIONS = """[{{"text":"Draft the vendor agreement and send it over","kind":"commitment","owner":"Speaker 1","quote":"I'll draft the vendor agreement by Friday and send it over","at":"00:01:10"}},
 {{"text":"Consolidate the renewal dates into one source","kind":"suggestion","owner":"","quote":"The renewal dates are scattered across three spreadsheets","at":"00:02:40"}}]

Note what was skipped: the weather remark is small talk, so it produced nothing."""

EXTRACT_PROMPT = """Read this conversation transcript and list the things that should end up
on a to-do list.

{rules}

Return a JSON array. Each element:
{{{{"text": "the action, imperative, one line",
  {kind_field}
  "owner": "who would do it, or empty string if unclear",
  "quote": "the transcript line it came from, copied exactly",
  "at": "the [HH:MM:SS] timestamp of that line"}}}}

Here is a worked example.

TRANSCRIPT:
[00:01:10] Okay, on the billing work, I'll draft the vendor agreement by Friday and send it over.
[00:02:05] Yeah, the weather has been strange lately.
[00:02:40] The renewal dates are scattered across three spreadsheets, it's a mess.

OUTPUT:
{example}

Guidance: skip small talk and pleasantries. This transcript comes from automatic speech
recognition, so if a line is too garbled to understand, skip it rather than guessing at
it. Every "quote" must be copied from the transcript below — never from this example.
Plenty of conversations contain nothing actionable — if this is one of them, return an
empty array [] rather than manufacturing something.

Return ONLY the JSON array, no prose and no code fences.

TRANSCRIPT:
{{chunk}}

OUTPUT:
"""


def build_prompt(*, suggestions: bool) -> str:
    return EXTRACT_PROMPT.format(
        rules=_RULES_WITH_SUGGESTIONS if suggestions else _RULES_COMMITMENTS_ONLY,
        kind_field=_KIND_FIELD_WITH_SUGGESTIONS if suggestions else _KIND_FIELD_COMMITMENTS_ONLY,
        example=_EXAMPLE_WITH_SUGGESTIONS if suggestions else _EXAMPLE_COMMITMENTS_ONLY,
    )

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


def extract_from_text(cfg: Config, text: str, *, suggestions: bool | None = None) -> list[dict]:
    if suggestions is None:
        suggestions = cfg.extract_suggestions
    prompt = build_prompt(suggestions=suggestions)
    found: list[dict] = []
    dropped = skipped_kind = 0
    for chunk in _chunk(text):
        raw = _generate(cfg, prompt.format(chunk=chunk))
        chunk_norm = _norm(chunk)
        for item in _parse_json_array(raw):
            if not _grounded(str(item.get("quote") or ""), chunk_norm):
                dropped += 1
                continue
            kind = str(item.get("kind") or "commitment").strip().lower()
            if kind not in ("commitment", "suggestion"):
                kind = "commitment"
            # Backstop. The prompt never mentions suggestions when they are off, but a
            # model that volunteers one anyway must not slip onto the board sideways.
            if kind == "suggestion" and not suggestions:
                skipped_kind += 1
                continue
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
    # Never silent: a filter you can't see is one you stop trusting.
    if dropped:
        print(f"    [dropped {dropped} with a quote not traceable to the transcript]")
    if skipped_kind:
        print(f"    [dropped {skipped_kind} suggestion(s) — commitments only; --suggestions to keep]")
    return unique


def run(
    cfg: Config,
    store: Store,
    *,
    limit: int | None = None,
    force: bool = False,
    suggestions: bool | None = None,
) -> dict:
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
    want = cfg.extract_suggestions if suggestions is None else suggestions
    scope = "commitments and suggestions" if want else "commitments"
    print(f"  {len(rows)} recordings to scan for {scope} · {cfg.llm_label}")

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
            for item in extract_from_text(cfg, text, suggestions=suggestions):
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
