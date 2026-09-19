"""A labelled set for the action board, so "which three" stops being an opinion.

Two candidate rankers have now been built for the budget D30 introduced — a lexical
rubric and a model selection pass — and neither could be validated, for the same
reason: the only ground truth in the archive is seven accepted actions against 975 that
were not, and 539 of those were dropped in bursts of ten or more. That is a person
clearing a board, not judging items. The negatives are unreliable and the positives are
seven.

So the number has to be made rather than found, and the design question is what to
label. Labelling *the ranker's picks* produces a set that rots the moment the ranker
changes — the mistake B14 exists to warn about. Labelling **the whole candidate pool**
for a sample of recordings produces a set that any future ranker can be scored against
offline, forever, without asking a person anything twice.

Two properties make that honest:

**Order is randomised.** A candidate list shown in extraction order anchors the judge on
whatever extraction happened to put first, and that is one of the things being measured.

**The verdict is per item, never per board.** "Would you put this on your list?" is
answerable about one sentence. "Is this board good?" is not, and a board-level verdict
is exactly the bulk drop that made the existing history useless.

The set lives at `{archive_root}/eval/actions.jsonl`, beside the transcripts it
describes and never in this repository — same reasoning as the retrieval golden set: a
commitment is as personal as the recording it came from.
"""

from __future__ import annotations

import json
import random
import time
from pathlib import Path

from .config import Config
from .store import Store

# A recording where the budget does not force a choice teaches nothing about choosing.
MIN_SURPLUS = 2


def judged_path(cfg: Config) -> Path:
    return cfg.archive_root / "eval" / "actions.jsonl"


def load_judged(cfg: Config) -> list[dict]:
    path = judged_path(cfg)
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def save_judged(cfg: Config, rows: list[dict]) -> None:
    path = judged_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))


def verdicts(cfg: Config) -> dict[int, str]:
    """action_id -> keep | drop. The last verdict for an action wins."""
    return {int(r["action_id"]): r["verdict"] for r in load_judged(cfg)
            if r.get("verdict") in ("keep", "drop")}


def sample(cfg: Config, store: Store, *, recordings: int = 5, seed: int = 0,
           max_items: int = 60, kinds_wanted: set[str] | None = None) -> list[dict]:
    """Pick recordings worth labelling, and return their pools in a shuffled order.

    Stratified across conversation kinds rather than taking the biggest lists: the
    budget behaves differently for an interview than for a working session, and a set
    drawn only from the longest lists would measure the pathological case only.

    `max_items` bounds the sitting rather than the sample. A set nobody finishes is
    worth less than a smaller one somebody does — and a labelling session abandoned
    halfway is how the bulk-drop history happened in the first place.
    """
    done = set(verdicts(cfg))
    by_kind: dict[str, list[str]] = {}
    for row in store.db.execute(
        "SELECT a.recording_id rid, COALESCE(k.kind, 'unclassified') kind, COUNT(*) n "
        "FROM actions a LEFT JOIN conversation_kinds k ON k.recording_id = a.recording_id "
        "GROUP BY a.recording_id HAVING n >= 3"
    ):
        by_kind.setdefault(row["kind"], []).append(row["rid"])

    rng = random.Random(seed)
    for rids in by_kind.values():
        rng.shuffle(rids)

    # Round-robin across kinds so a rare kind is represented before a common one is
    # exhausted.
    picked: list[str] = []
    order = sorted(by_kind)
    while len(picked) < recordings and any(by_kind[k] for k in order):
        for k in order:
            if kinds_wanted and k not in kinds_wanted:
                continue
            if by_kind[k] and len(picked) < recordings:
                picked.append(by_kind[k].pop())

    pools, budgeted = [], 0
    for rid in picked:
        rows = [dict(a) for a in store.actions(recording_id=rid)]
        if len(rows) < MIN_SURPLUS + 1:
            continue
        unlabelled = [a for a in rows if a["id"] not in done]
        if not unlabelled:
            continue
        if budgeted and budgeted + len(unlabelled) > max_items:
            continue
        rng.shuffle(rows)          # never show them in extraction order
        pools.append({"recording_id": rid, "candidates": rows})
        budgeted += len(unlabelled)
        if budgeted >= max_items:
            break
    return pools


def record(cfg: Config, rows: list[dict]) -> None:
    """Append verdicts, newest last. Append-only, like every other history here."""
    existing = load_judged(cfg)
    now = int(time.time())
    for r in rows:
        r.setdefault("judged_at", now)
    save_judged(cfg, existing + rows)


# ------------------------------------------------------------------ the number


def precision(chosen_ids: list[int], truth: dict[int, str]) -> dict:
    """Precision over the labelled subset of one board, and what was missed.

    Items with no verdict are excluded from the denominator rather than counted as
    wrong: an unlabelled item is not evidence, and treating it as a miss would make a
    half-labelled set look like a broken ranker.
    """
    labelled = [i for i in chosen_ids if i in truth]
    kept = [i for i in labelled if truth[i] == "keep"]
    return {
        "picked": len(chosen_ids),
        "labelled": len(labelled),
        "correct": len(kept),
        "precision": round(len(kept) / len(labelled), 3) if labelled else None,
    }


def score_board(chosen_ids: list[int], pool_ids: list[int], truth: dict[int, str]) -> dict:
    """One board, scored both ways.

    Precision is what the owner sees; recall is what the cut cost. Reporting only the
    first would make "keep nothing" look perfect, which is the failure mode a budget
    invites.
    """
    p = precision(chosen_ids, truth)
    keepers = [i for i in pool_ids if truth.get(i) == "keep"]
    found = [i for i in chosen_ids if truth.get(i) == "keep"]
    return {
        **p,
        "keepers_in_pool": len(keepers),
        "keepers_found": len(found),
        "recall": round(len(found) / len(keepers), 3) if keepers else None,
    }
