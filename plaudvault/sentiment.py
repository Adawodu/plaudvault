"""Score the emotional register of a transcript, locally, as an estimate.

Runs by default on everything that gets transcribed, because the value is in the
trend rather than in any single reading — one conversation's tone tells you almost
nothing, and a year of them tells you a great deal.

Three things keep this honest. It reads *automatic speech recognition*, which
carries no tone of voice, drops words and mangles names, so the model is asked for
its own `confidence` and the console never shows a reading without it. It scores
each segment separately and reduces, so a two-hour conversation that turned partway
through registers as `mixed` instead of averaging out to a bland neutral. And the
neutral band is wide: most conversation is unremarkable, and a reading near zero is
the correct and common answer, not a failure to detect something.
"""

from __future__ import annotations

import json
import re
import time

from .config import Config
from .llm import available
from .store import Store
from .summarize import _chunk, _generate
from .transcribe import read_transcript

# Below this there is not enough speech for a tone reading to mean anything. Such
# recordings are marked as looked-at and left unscored rather than given a number.
MIN_CHARS = 400

# |valence| inside this band is "no strong feeling either way" — not mild positivity.
# Deliberately wide: a narrow band turns ASR noise into a mood.
NEUTRAL_BAND = 0.15

# Segment valences that straddle both sides by at least this much make the whole
# recording `mixed`, rather than letting the two halves cancel into a false neutral.
MIXED_SPREAD = 0.35

LABELS = ("positive", "negative", "neutral", "mixed")

PROMPT = """Read this segment of a conversation transcript and judge its emotional register.

Return a single JSON object:
{{"valence": number from -1 to 1,
  "energy": number from 0 to 1,
  "label": "positive" or "negative" or "neutral" or "mixed",
  "confidence": number from 0 to 1,
  "drivers": ["short phrase", ...]}}

valence     how positive or negative the exchange feels. -1 is hostile or distressed,
            0 is neutral or purely factual, 1 is warm or delighted.
energy      how activated it is, independently of valence. 0 is flat and slow, 1 is
            heated or excited. An argument and a celebration are both high energy.
label       use "mixed" only when the segment genuinely swings both ways.
confidence  how sure you are. This is automatic speech recognition of real speech: it
            drops words, mangles names, and carries no tone of voice. If the text is
            thin, garbled, or pure logistics, say so with a low number rather than
            guessing confidently.
drivers     at most three short phrases naming what drove the reading. Quote or
            paraphrase the transcript. Do not speculate about anyone's inner life.

Here is a worked example.

SEGMENT:
[00:04:10] So the invoice is still wrong, that's the third month running.
[00:04:22] I know, I'm sorry, I'll get it fixed today.
[00:04:31] It's fine, I just don't want to keep chasing it.

OUTPUT:
{{"valence":-0.35,"energy":0.5,"label":"negative","confidence":0.7,"drivers":["invoice wrong three months running","having to keep chasing a fix"]}}

Most conversation is unremarkable. A neutral reading with a valence near 0 is a
correct answer and by far the most common one — do not manufacture drama, and do not
read a bad mood into someone discussing a difficult topic calmly.

Return ONLY the JSON object, no prose and no code fences.

SEGMENT:
{chunk}

OUTPUT:
"""


def _parse_object(raw: str) -> dict | None:
    raw = re.sub(r"^```(?:json)?|```$", "", (raw or "").strip(), flags=re.M).strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end < 0:
        return None
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _clamp(value, lo: float, hi: float, default: float | None = None) -> float | None:
    try:
        return max(lo, min(hi, float(value)))
    except (TypeError, ValueError):
        return default


def _score_segment(cfg: Config, chunk: str, n: int) -> dict | None:
    data = _parse_object(_generate(cfg, PROMPT.format(chunk=chunk)))
    if not data:
        return None
    valence = _clamp(data.get("valence"), -1, 1)
    if valence is None:
        # No usable number means no reading. Defaulting it to 0 would quietly file
        # a parse failure as "this conversation was neutral", which is a lie.
        return None
    label = str(data.get("label") or "").strip().lower()
    drivers = [str(d).strip()[:80] for d in (data.get("drivers") or []) if str(d).strip()]
    return {
        "n": n,
        "valence": round(valence, 3),
        "energy": round(_clamp(data.get("energy"), 0, 1, 0.5), 3),
        "label": label if label in LABELS else "neutral",
        # An unparseable confidence is treated as low, never as high.
        "confidence": round(_clamp(data.get("confidence"), 0, 1, 0.3), 3),
        "drivers": drivers[:3],
        "chars": len(chunk),
    }


def _aggregate(segments: list[dict]) -> dict:
    """Reduce per-segment readings to one, weighted by how much speech each covers."""
    weights = [max(s["chars"], 1) for s in segments]
    total = sum(weights)
    wmean = lambda key: sum(s[key] * w for s, w in zip(segments, weights)) / total  # noqa: E731

    valence = round(wmean("valence"), 3)
    values = [s["valence"] for s in segments]
    spread = round(max(values) - min(values), 3)

    if max(values) > NEUTRAL_BAND and min(values) < -NEUTRAL_BAND and spread >= MIXED_SPREAD:
        label = "mixed"
    elif valence > NEUTRAL_BAND:
        label = "positive"
    elif valence < -NEUTRAL_BAND:
        label = "negative"
    else:
        label = "neutral"

    drivers: list[str] = []
    seen: set[str] = set()
    for seg in segments:
        for d in seg["drivers"]:
            if d.lower() not in seen:
                seen.add(d.lower())
                drivers.append(d)

    return {
        "valence": valence,
        "energy": round(wmean("energy"), 3),
        "label": label,
        "confidence": round(wmean("confidence"), 3),
        "spread": spread,
        "drivers": drivers[:6],
        "segments": segments,
    }


def score_text(cfg: Config, text: str) -> dict | None:
    """One reading for a whole transcript, or None if nothing could be scored."""
    segments = [
        seg
        for i, chunk in enumerate(_chunk(text), 1)
        if (seg := _score_segment(cfg, chunk, i)) is not None
    ]
    return _aggregate(segments) if segments else None


def run(cfg: Config, store: Store, *, limit: int | None = None, force: bool = False) -> dict:
    rows = store.all() if force else store.needing_sentiment()
    rows = [r for r in rows if r["transcript_path"]]
    if limit:
        rows = rows[:limit]

    stats = {"scored": 0, "skipped": 0, "failed": 0}
    ok, why = available(cfg)
    if not ok:
        raise RuntimeError(f"language model unavailable — {why}")
    print(f"  {len(rows)} recordings to score for tone · {cfg.llm_label}")

    for i, row in enumerate(rows, 1):
        text = read_transcript(cfg, row["id"])
        if len(text.strip()) < MIN_CHARS:
            # Looked at, deliberately not scored. Marked so it is not re-tried daily.
            store.update(row["id"], sentiment_at=int(time.time()))
            stats["skipped"] += 1
            continue
        print(f"  [{i}/{len(rows)}] {row['filename'][:60]} ...", flush=True)
        try:
            result = score_text(cfg, text)
            if result is None:
                stats["failed"] += 1
                print("    [fail] no usable reading returned")
                continue
            store.set_sentiment(row["id"], model=cfg.llm_label, **result)
            store.update(row["id"], sentiment_at=int(time.time()))
            stats["scored"] += 1
            print(
                f"    {result['label']} · valence {result['valence']:+.2f} "
                f"· confidence {result['confidence']:.2f}"
            )
        except Exception as exc:  # noqa: BLE001
            stats["failed"] += 1
            print(f"    [fail] {exc}")

    return stats
