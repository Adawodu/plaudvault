"""Where one conversation ends and the next begins, inside a single file.

A pin left running all day produces one recording holding several unrelated
conversations. Everything downstream is per-conversation and all of it is wrong for
such a file: one title for five topics, one summary that averages them, one tone
reading, one conversation kind, and one action budget spread across material that has
nothing to do with itself. The three worst-yielding recordings in this archive — 69, 63
and 61 proposed actions — are all this shape, and the classifier is right to call them
`other`, because they genuinely are not one kind of thing.

**This is a view, not a split.** No audio is cut, copied or moved. A segment is a time
range with an identity, the file on disk stays exactly as it came off the device, and
deleting every segment row leaves the archive as it was. The recording remains the
master; segments are the working view over it.

**An unsegmented recording is one segment covering the whole file.** Not a special case
— that is what an unsegmented recording has always meant, made explicit. Every existing
recording has a valid segment list the day this ships, with nothing backfilled.

**Boundaries are proposed, then confirmed.** A proposal is derived and a re-run may
replace it. A confirmed boundary is precious and a re-run will not touch it, for the
same reason a confirmed speaker name survives diarization (D18) and a hand-set
conversation kind survives the classifier (D30): every decision downstream — a tier, an
accepted action, a brief — hangs off a boundary, and silently moving one orphans all of
them with nothing in the data saying when it happened.

**Silence is the signal. A change of cast was tried and is not.** Both looked equally
promising and only one survived contact with the archive. Scoring a turnover in the set
of speaker labels produced 90 conversations from one 4.5-hour recording and 19 from a
single 55-minute interview — because diarization labels are noisy and unnamed (this
archive holds 218 unnamed voices across 73 recordings), so the label set inside any
sliding window churns whether or not anybody left the room. It was measuring the
diarizer, not the conversation.

Silence alone, at three minutes, gives 5 conversations for a 4.5-hour recording and 4
for a 5-hour one, and leaves a 47-minute prayer session whole — which is correct, since
that recording's problem was always its kind and never its boundaries. Cast turnover is
still computed and still reported as evidence on a candidate, because it is real
information about the audio; it simply no longer creates a boundary by itself.

What silence cannot catch is one conversation ending and the next beginning with no
pause at all. That is a change of *subject*, which is visible only in what was said, so
it is a job for a model reading the transcript — and for a long recording, a model that
can hold the whole transcript at once.
"""

from __future__ import annotations

import json

from .config import Config
from .store import Store

# Silence long enough that the room has stopped. Below this, people are thinking; above
# it, something ended. Measured on this archive: 90s produced plausible-looking cuts
# every few minutes, 180s produces a handful per multi-hour file. Conservative on
# purpose — missing a boundary leaves a recording exactly as it is today, while
# inventing one cuts a conversation in half and puts its commitments in two places.
GAP_MS = 180_000

# A segment shorter than this is a fragment, not a conversation — a phone call taken in
# the middle of a meeting should not become a peer of the meeting.
MIN_SEGMENT_MS = 120_000

# Two long silences in a row leave a sliver between them that is mostly dead air. On a
# real recording one such span ran seven minutes and held 368 characters of speech.
# Duration says it is a conversation and the audio says nobody was talking, so speech
# time decides: a span with less than this much actual talking is joined to the one
# before it rather than standing as a conversation of its own.
MIN_SPEECH_MS = 60_000

# Reported as evidence, never as a cut on its own. See the module docstring: at this
# archive's diarization quality a sliding window's label set turns over constantly, so
# a threshold here selects noise. Kept because it is real information beside a silence
# that already stands on its own.
CAST_CHANGE = 0.6


def turns_path(cfg: Config, rec_id: str):
    return cfg.diarization_dir / f"{rec_id}.json"


def load_turns(cfg: Config, rec_id: str) -> list[dict]:
    """Diarization turns, oldest first. Empty when the recording was never diarized."""
    path = turns_path(cfg, rec_id)
    if not path.exists():
        return []
    turns = json.loads(path.read_text()).get("turns") or []
    return sorted(
        ({"start_ms": int(float(t["start"]) * 1000),
          "end_ms": int(float(t["end"]) * 1000),
          "speaker": str(t.get("speaker") or "")} for t in turns),
        key=lambda t: t["start_ms"],
    )


def _cast(turns: list[dict]) -> set[str]:
    return {t["speaker"] for t in turns if t["speaker"]}


def candidates(turns: list[dict], *, gap_ms: int = GAP_MS,
               cast_change: float = CAST_CHANGE) -> list[dict]:
    """Points where a conversation plausibly ends, each with why.

    Only a silence opens a candidate. Cast turnover rides along as evidence — it
    strengthens a candidate a silence already opened, and cannot open one itself.

    Returned as evidence rather than as a decision: a gap is a fact about the audio, and
    whether it amounts to a new conversation is a judgement about what was being said.
    The caller decides; this only says where to look.
    """
    if len(turns) < 2:
        return []
    found: list[dict] = []
    for i in range(1, len(turns)):
        gap = turns[i]["start_ms"] - turns[i - 1]["end_ms"]
        # The cast on each side, sampled over a window rather than a single turn: one
        # person saying "mhm" does not make a room.
        before = _cast(turns[max(0, i - 12):i])
        after = _cast(turns[i:i + 12])
        union = before | after
        turnover = len(before ^ after) / len(union) if union else 0.0

        if gap < gap_ms:
            continue
        cast_turned = turnover >= cast_change and len(union) > 1
        why = f"{gap // 1000}s silence"
        if cast_turned:
            why += f", cast changed {turnover:.0%}"
        found.append({
            "at_ms": turns[i]["start_ms"],
            "gap_ms": max(0, gap),
            "turnover": round(turnover, 3),
            # A longer silence is a stronger claim, and a cast that also turned over
            # strengthens it further — but neither invents a boundary without a gap.
            "confidence": round(min(1.0, 0.5 + min(0.3, gap / (gap_ms * 6))
                                    + (0.2 if cast_turned else 0.0)), 3),
            "why": why,
        })
    return found


def spans(duration_ms: int, cuts: list[dict], *,
          min_segment_ms: int = MIN_SEGMENT_MS) -> list[dict]:
    """Turn boundary points into segments covering the whole recording.

    Every millisecond of the recording belongs to exactly one segment. Gaps between
    segments would create audio that is in the archive but in no conversation, which is
    how a commitment goes missing without anything reporting it.

    A cut that would produce a fragment is dropped rather than merged forward: it is a
    weaker claim than the cut before it, and dropping it leaves the two conversations
    joined, which is today's behaviour and therefore safe.
    """
    if duration_ms <= 0:
        return []
    kept: list[dict] = []
    last = 0
    for c in sorted(cuts, key=lambda c: c["at_ms"]):
        at = int(c["at_ms"])
        if at - last < min_segment_ms or duration_ms - at < min_segment_ms:
            continue
        kept.append({"start_ms": last, "end_ms": at,
                     "confidence": c.get("confidence"), "why": c.get("why", "")})
        last = at
    if not kept:
        return []
    kept.append({"start_ms": last, "end_ms": duration_ms,
                 "confidence": None, "why": "end of recording"})
    return kept


def speech_ms(turns: list[dict], start_ms: int, end_ms: int) -> int:
    """How much of a span was somebody actually talking.

    Clipped to the span, so a turn straddling a boundary counts only the part inside
    it. Duration alone cannot tell a conversation from dead air between two silences.
    """
    total = 0
    for t in turns:
        lo, hi = max(t["start_ms"], start_ms), min(t["end_ms"], end_ms)
        if hi > lo:
            total += hi - lo
    return total


def join_quiet(spans_: list[dict], turns: list[dict], *,
               min_speech_ms: int = MIN_SPEECH_MS) -> list[dict]:
    """Fold near-silent spans into their neighbour.

    Joined backwards into the preceding conversation rather than dropped, because the
    audio has to stay covered: a span that belongs to no segment is archive nobody can
    reach through the working view.
    """
    if not spans_:
        return []
    out: list[dict] = []
    for sp in spans_:
        quiet = speech_ms(turns, sp["start_ms"], sp["end_ms"]) < min_speech_ms
        if quiet and out:
            out[-1]["end_ms"] = sp["end_ms"]
            if "quiet stretch" not in out[-1]["why"]:
                out[-1]["why"] += " (+ a quiet stretch)"
        else:
            out.append(dict(sp))
    # A single span left over is not a segmentation — it is the whole recording, which
    # is what an unsegmented one already means.
    return out if len(out) > 1 else []


def propose(cfg: Config, store: Store, rec_id: str, *, gap_ms: int = GAP_MS,
            min_segment_ms: int = MIN_SEGMENT_MS,
            min_speech_ms: int = MIN_SPEECH_MS) -> dict:
    """Propose a segmentation for one recording from its diarization.

    Returns the spans and the evidence. Writes nothing — the caller decides whether a
    proposal becomes state, and a person decides whether it becomes permanent.
    """
    rec = store.get(rec_id)
    if rec is None:
        return {"spans": [], "reason": "no such recording"}
    duration_ms = int((rec["duration_s"] or 0) * 1000)
    turns = load_turns(cfg, rec_id)
    if not turns:
        return {"spans": [], "reason": "not diarized — no turns to read boundaries from"}
    cuts = candidates(turns, gap_ms=gap_ms)
    found = join_quiet(spans(duration_ms, cuts, min_segment_ms=min_segment_ms),
                       turns, min_speech_ms=min_speech_ms)
    return {
        "spans": found,
        "candidates": cuts,
        "reason": "one conversation" if not found else f"{len(found)} conversations",
    }


def transcript_for(text: str, start_ms: int, end_ms: int) -> str:
    """The lines of a timestamped transcript that fall inside a segment.

    The transcript is not cut on disk either. This reads the master and returns a view
    of it, so a segment's text is always exactly what the recording says and can never
    drift from it.
    """
    out = []
    for line in text.splitlines():
        if not line.startswith("["):
            continue
        stamp = line[1:9]
        parts = stamp.split(":")
        if len(parts) != 3 or not all(p.strip().isdigit() for p in parts):
            continue
        at = (int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])) * 1000
        if at < start_ms:
            continue
        if at >= end_ms:
            break
        out.append(line)
    return "\n".join(out)
