"""What kind of conversation was this, and how much should it be expected to yield?

Extraction ran the same way on every recording, and the archive says what that costs:
a median of 12 actions per conversation against a wanted 2-3, a maximum of 69, and 567
of 982 dropped by hand — 539 of those in bursts of ten or more, which is not triage but
clearing the board. A prayer session produced 69 action items. Nothing was broken; the
extractor was asked fifteen times, once per chunk, "what commitments are here?", and a
leading question gets answered.

The missing fact is the cheapest one available: a prayer is not a standup. So one call
per *recording* — against the summary, which already exists — puts it in a fixed
vocabulary, and the kind carries a budget. Kinds that yield nothing are not extracted at
all, which is a different outcome from extracting and finding nothing: the console can
say "no actions expected from a devotional" rather than showing an empty board that
looks like a failure.

**The vocabulary is fixed, not learned.** A learned set would drift with the corpus and
take the budgets, the console labels and the MCP contract with it. Six kinds a person
can hold in their head is a schema; a clustering is a snapshot. An unfamiliar
conversation lands on `other`, which is budgeted like a working session rather than
silently dropped.
"""

from __future__ import annotations

import json
import re

from . import segment as segment_mod
from .config import Config
from .llm import available  # noqa: F401  — re-exported so callers gate on one thing
from .store import Store
from .summarize import _generate, summary_path
from .transcribe import read_transcript

# kind -> (budget, what it is). The budget is the most actions extraction may put on
# the board for such a conversation; 0 means do not extract at all.
KINDS: dict[str, tuple[int, str]] = {
    "working":     (3, "a work conversation: decisions, planning, a standup, a client call"),
    "product":     (3, "designing or specifying something to be built, researched or executed"),
    "interview":   (1, "a job interview, screening call or performance conversation, in "
                       "either direction — much of it is a CV or a job description read aloud"),
    "personal":    (2, "family, friends, logistics, health, money — real commitments, small ones"),
    # Was 0 until the archive said otherwise: a sermon on spiritual leadership produced
    # "commit to breaking bread with someone at least once a month", which its owner is
    # acting on. A teaching conversation is not a working session, but it is not empty
    # either — one, so the rare real commitment has a place to land.
    "devotional":  (1, "prayer, worship, scripture, reflection, a sermon or teaching"),
    # Still 0, and on firmer ground: nobody in the room is speaking. Across the corpus
    # no action from a `media` recording has ever been accepted.
    "media":       (0, "a recording of something playing: a video, a podcast, a "
                       "lecture, background audio nobody in the room is committing to"),
    "other":       (3, "none of the above"),
}
DEFAULT_KIND = "other"

# A product conversation is the one case where the ceiling is wrong: it is meant to
# produce something an agent can be handed. It still gets a small number of *actions* —
# the brief that belongs beside them is a separate artifact and not built yet.
PRODUCT_KINDS = frozenset({"product"})


def budget(kind: str | None) -> int:
    return KINDS.get((kind or DEFAULT_KIND), KINDS[DEFAULT_KIND])[0]


def extractable(kind: str | None) -> bool:
    """Is this the kind of conversation that can contain a commitment at all?"""
    return budget(kind) > 0


_VOCAB = "\n".join(f"- {k}: {desc}" for k, (_, desc) in KINDS.items())

# Built by concatenation, and filled with `.replace` rather than `.format`: the reply
# template is JSON, and every brace in it is literal. Doubling them to survive a format
# call is the kind of detail that silently rots the day somebody edits the prompt.
PROMPT = (
    "You are classifying one conversation so that a task extractor knows whether to run "
    "on it at all.\n\n"
    "Choose exactly one kind from this list:\n"
    + _VOCAB
    + "\n\nJudge what the recording IS, not what it mentions. A prayer that mentions work "
    "is devotional. A job interview where a candidate describes past projects is an "
    "interview, not a working session — the projects are being recounted, not planned. A "
    "recording of a podcast playing is media even when the topic is business.\n\n"
    "Reply with JSON only, no other text:\n"
    '{"kind": "<one of: ' + ", ".join(KINDS) + '>", "confidence": <0.0-1.0>, '
    '"why": "<at most 12 words>"}\n\n'
    "CONVERSATION:\n{body}\n"
)


def _parse(raw: str) -> dict | None:
    raw = re.sub(r"^```(?:json)?|```$", "", (raw or "").strip(), flags=re.M).strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end < 0:
        return None
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    kind = str(data.get("kind") or "").strip().lower()
    if kind not in KINDS:
        # An invented kind is not silently coerced to a working session: that would
        # hand a budget of 3 to something nobody classified. `other` is the honest
        # landing place and carries the same budget by design, but says what happened.
        return {"kind": DEFAULT_KIND, "confidence": 0.0,
                "why": f"model returned {kind[:24]!r}, not in the vocabulary"}
    conf = data.get("confidence")
    try:
        conf = max(0.0, min(1.0, float(conf)))
    except (TypeError, ValueError):
        conf = 0.3  # unparseable confidence is low, never high
    return {"kind": kind, "confidence": round(conf, 3),
            "why": str(data.get("why") or "").strip()[:120]}


# How much of a segment's transcript to read when classifying it. The opening says what
# a conversation is for and the closing says how it ended, and between them sits the
# material that makes a long recording look like every kind at once.
HEAD_CHARS = 3500
TAIL_CHARS = 1500


def body_for(cfg: Config, store: Store, conv: dict) -> str:
    """The text to classify one conversation from.

    A recording that holds one conversation has a summary, and a summary is the better
    evidence: it is already written, it is dense, and what a conversation *was* survives
    summarising far better than what was committed in it does.

    A segment has no summary of its own — summaries are per recording, and a summary of
    a file holding a standup and a school run describes neither. So a segment is
    classified from its own slice of the transcript, read at both ends: the opening says
    what the conversation is for, the closing says how it ended.
    """
    if not conv["segmented"]:
        sp = summary_path(cfg, conv["recording_id"])
        if sp.exists():
            return sp.read_text()
        # No summary is not the same as nothing to read. A recording under
        # `summarize_min_seconds` never gets one, and falling through to "" left ten
        # short recordings permanently unclassifiable: queued by `needing_kind` on
        # every run, skipped on every run, and reported as outstanding work forever.
        # They are short by definition, so the transcript is the better evidence
        # anyway — it is most of what a summary of them would have said.
    text = segment_mod.transcript_for(
        read_transcript(cfg, conv["recording_id"]), conv["start_ms"], conv["end_ms"])
    if not conv["segmented"]:
        text = read_transcript(cfg, conv["recording_id"])
    if len(text) <= HEAD_CHARS + TAIL_CHARS:
        return text
    return f"{text[:HEAD_CHARS]}\n\n[... middle of the conversation omitted ...]\n\n{text[-TAIL_CHARS:]}"


def classify_text(cfg: Config, body: str, *, tier: str | None = None) -> dict | None:
    """Classify one conversation, or None if nothing usable came back."""
    if not body.strip():
        return None
    return _parse(_generate(cfg, PROMPT.replace("{body}", body[:6000]), tier=tier))


def run(cfg: Config, store: Store, *, limit: int | None = None, force: bool = False) -> dict:
    """Classify conversations that have no kind yet. A human's kind is never overwritten.

    Per conversation, not per recording: a file holding a standup and a school run holds
    two kinds, and calling the file one of them is the mistake this view exists to fix.
    """
    rows = store.needing_kind(force=force)
    if limit:
        rows = rows[:limit]

    stats = {"classified": 0, "skipped": 0, "failed": 0, "kinds": {}}
    print(f"  {len(rows)} conversations to classify · {cfg.llm_label}")

    for i, conv in enumerate(rows, 1):
        body = body_for(cfg, store, conv)
        if not body.strip():
            # Classifying from nothing would invent a kind, and the kind decides
            # whether extraction runs at all. Left unclassified until summarised.
            stats["skipped"] += 1
            continue
        print(f"  [{i}/{len(rows)}] {conv['label'][:58]} ...", flush=True)
        try:
            t = store.triage_of(conv["recording_id"])
            got = classify_text(cfg, body, tier=(t["tier"] if t else None))
            if got is None:
                stats["failed"] += 1
                print("    [fail] no usable classification returned")
                continue
            store.set_kind(conv["recording_id"], segment_idx=conv["segment_idx"],
                           kind=got["kind"], confidence=got["confidence"],
                           why=got["why"], source="model", model=cfg.llm_label)
            stats["classified"] += 1
            stats["kinds"][got["kind"]] = stats["kinds"].get(got["kind"], 0) + 1
            print(f"    {got['kind']} · budget {budget(got['kind'])} "
                  f"· confidence {got['confidence']:.2f} — {got['why']}")
        except Exception as exc:  # noqa: BLE001
            stats["failed"] += 1
            print(f"    [fail] {exc}")

    return stats
