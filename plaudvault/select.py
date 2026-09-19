"""Which of these twelve belong on a list of three.

D30 gave every conversation a budget and deliberately stopped short of spending it.
This spends it.

**A rubric was built first, measured, and thrown away.** The obvious design — score each
candidate on concreteness, intent markers, a time expression — was implemented and run
against the only ground truth this archive holds: the seven actions its owner actually
kept, against the 975 he did not. It put one of the seven inside its budget. Measured as
mean position within each candidate list (0 = top, 1 = bottom), it scored 0.382 against
0.449 for extraction order and 0.487 for random. Better than nothing, and nowhere near
good enough to pick three.

The reason it failed is the useful part. *"Define the compensation band for the role"*
was dropped; *"Identify the target audience for the app"* was kept. They are the same
sentence grammatically — same concreteness, same absent time, same abstract-ish object.
What separates them is whose commitment it is and whether it was decided or merely
aired, and neither of those is visible in the sentence. **The information a ranker needs
is in the conversation, not in the candidate.**

So selection is one model call per *recording*, with the summary in the prompt and every
candidate visible at once. Three reasons that shape is right rather than a per-item
judge:

1. **Comparative, not absolute.** A small model asked "is this a real commitment?" about
   one sentence is guessing; asked "which three of these fourteen are real" it is
   choosing, and choosing is the thing it is good at.
2. **It costs one call.** Extraction already makes ~15 per recording, so the precision
   pass adds about 7% — the objection that sank a per-item second pass does not apply.
3. **It can be told the budget.** The number is the product decision; the model is only
   asked to honour it.

**Permission to return nothing is the most important line in the prompt.** Extraction
over-produces because it asks each chunk "what commitments are here?", and a leading
question gets answered. Selection is told explicitly that most conversations contain one
or two real commitments and that returning none is a correct answer.

Nothing is deleted. Everything not selected is kept as `overflow` with the model's
reason attached, because the cut is a judgement about a dozen sentences, not a fact, and
a filter you cannot see is one you stop trusting.
"""

from __future__ import annotations

import json
import re

from .config import Config
from .summarize import _generate

# Guidance drawn from the corpus rather than invented: among 565 dropped actions the
# most common opening verbs were walk (15), discuss (14), clarify (11) and share (11) —
# verbs describing the conversation that was happening, not work arising from it.
RULES = """Keep an item only if someone actually committed to doing it, or was asked to
do it and agreed. Everything else is conversation.

Reject an item that describes the conversation itself — "walk me through X",
"discuss Y", "clarify Z", "share the screen". These are the single most common kind of
mistake in this archive.
Reject aspirations and rhetoric: "make this a reality", "unlock opportunities",
"become FHIR native", "ask the right questions".
Reject anything that was being recited rather than decided — a job description read
aloud, a document reviewed, a video or podcast playing in the room.
Reject a process step that was explained but not adopted by anyone present.
Prefer an item with a bounded object, and one with a time over one without.

It is correct to return fewer than the budget. It is correct to return none. Most
conversations contain one or two real commitments, not twelve — a list of twelve is the
mistake this step exists to correct."""


def _prompt(*, summary: str, kind: str, budget: int, candidates: list[dict]) -> str:
    lines = []
    for i, c in enumerate(candidates, 1):
        quote = " ".join(str(c.get("quote") or "").split())[:240]
        owner = str(c.get("owner") or "").strip()
        who = f" [said by {owner}]" if owner else ""
        lines.append(f'{i}. {str(c.get("text") or "").strip()[:200]}{who}\n   said: "{quote}"')
    return (
        "You are choosing which proposed commitments from one conversation are worth "
        "putting on a person's list.\n\n"
        f"WHAT THIS CONVERSATION WAS ({kind}):\n{summary.strip()[:2500]}\n\n"
        f"AT MOST {budget} may be kept.\n\n"
        f"RULES:\n{RULES}\n\n"
        f"CANDIDATES:\n" + "\n".join(lines) + "\n\n"
        'Reply with JSON only, no other text:\n'
        '{"keep": [{"n": <candidate number>, "why": "<at most 10 words>"}]}\n'
        'An empty list is a valid and often correct answer: {"keep": []}\n'
    )


def _parse(raw: str, *, n_candidates: int, budget: int) -> list[dict] | None:
    """Read the model's choice, or None if it did not make one.

    None and `[]` are different answers and must not collapse: `[]` is "none of these
    are real", which is frequently correct, while None is "the model did not reply
    usefully" and must leave the candidates alone rather than silently hiding all of
    them.
    """
    raw = re.sub(r"^```(?:json)?|```$", "", (raw or "").strip(), flags=re.M).strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end < 0:
        return None
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("keep"), list):
        return None

    chosen: list[dict] = []
    seen: set[int] = set()
    for item in data["keep"]:
        if isinstance(item, int):
            item = {"n": item}
        if not isinstance(item, dict):
            continue
        try:
            n = int(item.get("n"))
        except (TypeError, ValueError):
            continue
        # An index outside the list is the model inventing a candidate. Dropped
        # rather than clamped: clamping would silently promote whatever happens to
        # sit at that position.
        if not (1 <= n <= n_candidates) or n in seen:
            continue
        seen.add(n)
        chosen.append({"index": n - 1, "why": str(item.get("why") or "").strip()[:120]})
    # Over budget is the model ignoring the one number it was given. Its order is its
    # preference, so the surplus comes off the end.
    return chosen[:budget]


def choose(cfg: Config, candidates: list[dict], *, summary: str, kind: str,
           budget: int, tier: str | None = None) -> dict:
    """Split candidates into kept and overflow. Never discards, never invents.

    Returns `{"kept": [...], "overflow": [...], "decided": bool}`. `decided` is False
    when the model gave no usable answer — the caller then keeps everything rather than
    hiding a whole conversation's work behind a failed parse.
    """
    if not candidates:
        return {"kept": [], "overflow": [], "decided": True}
    if budget <= 0:
        return {"kept": [], "overflow": list(candidates), "decided": True}
    if len(candidates) <= budget:
        # Already inside the budget. Asking anyway would spend a call to re-litigate
        # extraction's output, and this step exists to cut a list down, not to
        # second-guess a short one.
        return {"kept": list(candidates), "overflow": [], "decided": True}

    raw = _generate(cfg, _prompt(summary=summary, kind=kind, budget=budget,
                                candidates=candidates), tier=tier)
    picked = _parse(raw, n_candidates=len(candidates), budget=budget)
    if picked is None:
        return {"kept": list(candidates), "overflow": [], "decided": False}

    keep_at = {p["index"]: p["why"] for p in picked}
    kept, overflow = [], []
    for i, c in enumerate(candidates):
        if i in keep_at:
            kept.append({**c, "selection_note": keep_at[i],
                         "selection_rank": [p["index"] for p in picked].index(i) + 1})
        else:
            overflow.append({**c, "selection_note": "not selected", "selection_rank": None})
    # The model's own order is its preference and is preserved.
    kept.sort(key=lambda a: a["selection_rank"])
    return {"kept": kept, "overflow": overflow, "decided": True}
