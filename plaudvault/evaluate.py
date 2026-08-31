"""Measure retrieval, so claims about it stop being vibes.

Everything downstream of the index — semantic search, the MCP server answering an
agent's question, and every citation either of them hands back — rests on retrieval
quality that has never been measured. That was tolerable while a human read the hits
and judged them. It stopped being tolerable when a model started reading them and
writing an answer, because a confident answer built on a bad hit looks exactly like a
good one.

**The golden set lives in the archive, not the repo.** A query like "what did the
consultant say about the equity split" is as personal as the recording it points at,
and this repository is public. `{archive_root}/eval/golden.jsonl` sits beside the
transcripts it describes and is covered by whatever protects them.

**Generated queries are proposals, exactly like extracted actions.** `eval build` asks
a model to invent questions from the summaries; those land `verified: false` and are
excluded from the headline number until a human confirms them. That is not ceremony —
a generated query is scored against the recording it was generated *from*, so an
unverified set measures "can retrieval find the document that produced this question",
which is a strictly easier task than the one you care about.

**The known bias, stated up front.** Questions written from a transcript tend to reuse
its vocabulary, and lexical overlap flatters an embedding model. Real recall is the
case this harness is worst at: you remember "someone said they were underpaid" and the
recording says "not exactly making it worth my while". The generation prompt fights
this deliberately, and the reported number should still be read as an *upper* bound.
A harness that overstates retrieval is worse than no harness, so the caveat is printed
with every result rather than filed in a doc.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .config import Config
from .llm import available as llm_available
from .search import CHUNK_CHARS, OVERLAP_CHARS, _prefix, available, search
from .store import Store
from .summarize import _generate, summary_path

# Deep enough that a query landing at 30 is recorded as "found, badly" rather than
# collapsing into the same bucket as "not found at all". The distinction matters:
# the first is a ranking problem, the second is a recall problem, and they have
# different fixes.
SEARCH_DEPTH = 50
REPORT_AT = (1, 3, 5, 10)


def golden_path(cfg: Config) -> Path:
    return cfg.archive_root / "eval" / "golden.jsonl"


def results_dir(cfg: Config) -> Path:
    return cfg.archive_root / "eval" / "results"


# --------------------------------------------------------------------- the set


def load_golden(cfg: Config) -> list[dict]:
    path = golden_path(cfg)
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(json.loads(line))
    return out


def save_golden(cfg: Config, rows: list[dict]) -> None:
    path = golden_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")


# Two kinds of query, because they measure different things and mixing them hides both.
#
# A `direct` query is what someone usually types: it carries a name, a company, a
# number. Embedding search finds those almost regardless of how good it is, so a set
# made only of these saturates at recall@1 = 1.0 and can no longer detect a regression
# or separate two configurations — measured here on the first six generated queries,
# which is why the split exists.
#
# An `oblique` query is the case retrieval actually exists for and is worst at: you
# remember the gist and none of the words. "Was I underpaid" against a transcript that
# says "not exactly making it worth my while". That number is the one that moves.
# The worked examples, named once so the leak filter below and the prompt can never
# disagree about what they are. D9 found the same failure in extraction: a small model
# sometimes returns the prompt's own example instead of reading the input, because the
# example is the one thing in a prompt that looks exactly like a correct answer.
# Measured on the first full build here: 56 of 96 generated queries — 58% — were these
# strings, which would have produced a confidently meaningless oblique score.
_EXAMPLES = (
    "was I underpaid",
    "when did we agree to kill the old importer",
    "Why did we drop the second vendor?",
)

_KIND_RULES = {
    "direct": """Write them the way the person would actually type them months later.
Naming a person, company, place or number from the recording is fine and expected.""",
    "oblique": f"""Write them as somebody who remembers what the conversation was ABOUT
but none of the words in it:
- Use NO proper nouns. No person, company, product or place names.
- Use NO distinctive numbers, dates or figures from the text.
- Do not reuse the summary's own vocabulary. Reach for the everyday phrase instead:
  if the summary says "compensation was not competitive", write "{_EXAMPLES[0]}".
  If it says "we deprecated the legacy ingestion path", write
  "{_EXAMPLES[1]}".
- Those two are ILLUSTRATIONS OF STYLE. Never return them. Every question you write
  must be about the summary below and nothing else.""",
}

BUILD_PROMPT = """Below is a summary of one audio recording.

Write {n} questions that this recording — and ideally only this recording — answers.

{rules}

In every case:
- Ask about the substance: a decision, a disagreement, a commitment, a problem someone
  raised. Not the topic.
- "What was discussed?" is useless. "{example}" is a question.
- Never name the recording, its date, or its title.
- If the recording is too thin to ask {n} real questions about, write fewer.

Return ONLY a JSON array of strings, no prose and no code fences.

SUMMARY:
{summary}

QUESTIONS:"""


def _norm_query(q: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", (q or "").lower()).split())


def is_leaked_example(q: str) -> bool:
    """Did the model hand back the prompt's own worked example instead of reading?

    Checked both ways round, because it returns the example verbatim sometimes and a
    fragment of it other times. Either way the query is about nothing in the corpus,
    and scoring it against a recording it was never derived from measures noise while
    looking exactly like a measurement.
    """
    n = _norm_query(q)
    if len(n) < 6:
        return True
    return any(n == e or n in e or e in n for e in (_norm_query(x) for x in _EXAMPLES))


def _parse_questions(raw: str) -> list[str]:
    raw = (raw or "").strip()
    start, end = raw.find("["), raw.rfind("]")
    if start < 0 or end < 0:
        return []
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return []
    return [q.strip() for q in data if isinstance(q, str) and len(q.strip()) > 12]


def build(cfg: Config, store: Store, *, per_recording: int = 2,
          limit: int | None = None, kinds: tuple[str, ...] = ("direct", "oblique")) -> dict:
    """Propose queries from the corpus. Everything lands unverified.

    Only recordings with a summary are used: a summary is a compressed statement of
    what a recording was *about*, which is what a question should be aimed at, and it
    keeps a 90-minute transcript from having to fit in a prompt.
    """
    ok, why = llm_available(cfg)
    if not ok:
        raise RuntimeError(f"language model unavailable — {why}")

    existing = load_golden(cfg)
    seen = {(r["recording_id"], r["query"].lower()) for r in existing}

    rows = [r for r in store.visible() if summary_path(cfg, r["id"]).exists()]
    if limit:
        rows = rows[:limit]

    added = leaked = 0
    print(f"  proposing questions from {len(rows)} summarized recordings · {cfg.llm_label}")
    for i, row in enumerate(rows, 1):
        summary = summary_path(cfg, row["id"]).read_text()
        made = 0
        for kind in kinds:
            try:
                raw = _generate(
                    cfg,
                    BUILD_PROMPT.format(
                        n=per_recording, rules=_KIND_RULES[kind],
                        example=_EXAMPLES[2], summary=summary[:6000],
                    ),
                    timeout=180,
                )
            except Exception as exc:  # noqa: BLE001
                print(f"  [{i}/{len(rows)}] [fail:{kind}] {exc}")
                continue
            for q in _parse_questions(raw)[:per_recording]:
                if is_leaked_example(q):
                    leaked += 1
                    continue
                if (row["id"], q.lower()) in seen:
                    continue
                seen.add((row["id"], q.lower()))
                existing.append(
                    {
                        "query": q,
                        "recording_id": row["id"],
                        "recording": row["title"] or row["filename"],
                        "kind": kind,
                        "source": "generated",
                        "verified": False,
                        "created_at": int(time.time()),
                    }
                )
                added += 1
                made += 1
        # Written after every recording, not once at the end. This is ~2 model calls
        # per recording over the whole corpus; losing twenty minutes of generation to
        # a crash on the last one is avoidable, and the file is small enough that
        # rewriting it each time costs nothing.
        save_golden(cfg, existing)
        print(f"  [{i}/{len(rows)}] +{made}  {(row['title'] or row['filename'])[:48]}")

    kept, ambiguous = drop_ambiguous(existing)
    save_golden(cfg, kept)
    return {"added": added, "leaked": leaked, "ambiguous": ambiguous,
            "total": len(kept),
            "verified": sum(1 for r in kept if r.get("verified"))}


def drop_ambiguous(rows: list[dict]) -> tuple[list[dict], int]:
    """Remove any query proposed for more than one recording.

    The general form of the leak check, and the one that catches what an explicit list
    cannot anticipate: whatever produced it, a question asked of three different
    recordings cannot discriminate between them, so scoring it against one is
    meaningless. A query a human verified is kept regardless — that is a judgement, and
    judgements are not overruled by a heuristic.
    """
    counts: dict[str, int] = {}
    for r in rows:
        key = _norm_query(r["query"])
        counts[key] = counts.get(key, 0) + 1
    kept = [r for r in rows if counts[_norm_query(r["query"])] == 1 or r.get("verified")]
    return kept, len(rows) - len(kept)


# ------------------------------------------------------------------ measuring


def config_fingerprint(cfg: Config, store: Store) -> dict:
    """What a score is only comparable *within*.

    Retrieval numbers from different embedding models, chunk sizes or corpus sizes are
    not comparable, and a results file that does not say which produced it invites
    exactly that mistake six months later.
    """
    stats = store.index_stats(cfg.embed_model)
    return {
        "embed_model": cfg.embed_model,
        "chunk_chars": CHUNK_CHARS,
        "overlap_chars": OVERLAP_CHARS,
        "query_prefix": _prefix(cfg, "query") or None,
        "chunks": stats["chunks"],
        "corpus_recordings": stats["recordings"],
    }


def _rank_of(hits: list[dict], recording_id: str) -> int | None:
    """1-based position of the first hit from the expected recording, or None."""
    for i, h in enumerate(hits, 1):
        if h["recording_id"] == recording_id:
            return i
    return None


def run(cfg: Config, store: Store, *, verified_only: bool = True,
        depth: int = SEARCH_DEPTH) -> dict:
    ok, why = available(cfg)
    if not ok:
        raise RuntimeError(f"embedding model unavailable — {why}")

    golden = load_golden(cfg)
    if not golden:
        raise RuntimeError(
            f"no golden set at {golden_path(cfg)} — run: plaudctl eval build"
        )
    rows = [g for g in golden if g.get("verified")] if verified_only else golden
    if not rows:
        raise RuntimeError(
            f"{len(golden)} queries, none verified. Generated queries are proposals: "
            "confirm the good ones (plaudctl eval review) or measure them anyway with "
            "--unverified, knowing the number is optimistic."
        )

    per_query, missing = [], 0
    for g in rows:
        hits = search(cfg, store, g["query"], k=depth)
        rank = _rank_of(hits, g["recording_id"])
        if rank is None:
            missing += 1
        per_query.append(
            {
                "query": g["query"],
                "recording_id": g["recording_id"],
                "recording": g.get("recording", ""),
                "kind": g.get("kind", "direct"),
                "rank": rank,
                "top_score": hits[0]["score"] if hits else None,
                "verified": bool(g.get("verified")),
            }
        )

    metrics = _score(per_query)
    metrics["not_found"] = missing
    by_kind = {
        kind: _score([p for p in per_query if p["kind"] == kind])
        for kind in sorted({p["kind"] for p in per_query})
    }

    return {
        "at": int(time.time()),
        "config": config_fingerprint(cfg, store),
        "verified_only": verified_only,
        "metrics": metrics,
        "by_kind": by_kind,
        # A set that scores 1.000 at rank 1 has stopped being an instrument: it cannot
        # detect a regression and it cannot separate two configurations, because there
        # is no headroom in either direction. Said here rather than left for the reader
        # to infer from a row of full bars.
        "saturated": metrics["queries"] >= 5 and metrics["recall@1"] >= 0.98,
        "per_query": sorted(per_query, key=lambda p: (p["rank"] is None, p["rank"] or 0)),
    }


def _score(rows: list[dict]) -> dict:
    n = len(rows)
    if not n:
        return {"queries": 0}
    found = [p for p in rows if p["rank"]]
    out = {
        f"recall@{k}": round(sum(1 for p in found if p["rank"] <= k) / n, 4)
        for k in REPORT_AT
    }
    # MRR over the whole set: a query that never appears contributes 0, which is the
    # honest treatment — averaging only over the ones that were found would hide
    # exactly the failures this exists to surface.
    out["mrr"] = round(sum(1 / p["rank"] for p in found) / n, 4)
    out["queries"] = n
    return out


def save_result(cfg: Config, result: dict, *, label: str = "") -> Path:
    d = results_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(result["at"]))
    path = d / f"{stamp}{'-' + label if label else ''}.json"
    path.write_text(json.dumps(result, indent=1, ensure_ascii=False))
    return path


def latest_results(cfg: Config, n: int = 2) -> list[dict]:
    d = results_dir(cfg)
    if not d.exists():
        return []
    files = sorted(d.glob("*.json"), reverse=True)[:n]
    return [json.loads(f.read_text()) for f in files]


def compare(before: dict, after: dict) -> dict:
    """Two runs, and whether they can honestly be compared at all."""
    keys = ("embed_model", "chunk_chars", "overlap_chars", "query_prefix")
    changed = {
        k: (before["config"].get(k), after["config"].get(k))
        for k in keys
        if before["config"].get(k) != after["config"].get(k)
    }
    corpus_moved = before["config"].get("chunks") != after["config"].get("chunks")
    deltas = {
        k: round(after["metrics"][k] - before["metrics"].get(k, 0), 4)
        for k in after["metrics"]
        if isinstance(after["metrics"][k], float)
    }
    return {
        "changed_config": changed,
        "corpus_moved": corpus_moved,
        "deltas": deltas,
        # Two things moved at once, so the delta cannot be attributed to either. Said
        # plainly rather than left for the reader to notice.
        "confounded": bool(changed) and corpus_moved,
    }
