"""Who spoke, and which of them you already know.

Whisper returns text with timestamps and no notion of speakers, so a transcript of a
two-person conversation reads as one undifferentiated monologue. That costs more than
readability: an extracted commitment has an `owner` field the model can only guess at,
and a tone score cannot tell your frustration from someone else's.

This module runs pyannote over the audio, gets anonymous turns (`SPEAKER_00`) plus one
embedding per speaker, aligns those turns to the whisper segments already on disk, and
writes a transcript that says who is talking.

**The identity half is the point.** Anonymous labels are per-recording and useless
across an archive — `SPEAKER_00` is a different person in every file. When you name a
label, the embedding behind it is kept as that person's *voiceprint*, and the next
recording matches against it. You name Bayo once; every later recording finds him.

Three rules keep that from drifting:

1. **Only human confirmations build a voiceprint.** An automatic match is recorded as
   `source='voiceprint'` and never feeds back into the mean. Otherwise one bad match
   compounds: the identity slowly becomes whoever the machine has been mistaking for
   you, and nothing in the data says when it went wrong.
2. **Re-running diarization never un-names anybody.** Attribution lives in its own
   column and human decisions survive a re-run, exactly as triage survives a re-sync.
3. **The rendered transcript is derived, never authoritative.** Names are re-applied
   from the database on demand, so correcting one attribution rewrites every place that
   name appears without touching the diarization.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .config import Config
from .store import Store
from .transcribe import _format_ts, transcript_paths

# pyannote's models are gated: free, but each needs its licence accepted while signed
# in. Listing both means the error can say which page to open rather than "401".
GATED_MODELS = (
    "pyannote/speaker-diarization-community-1",
    "pyannote/segmentation-3.0",
)

_PIPELINE = None  # loading the models takes ~10s; one process, one load


# ---------------------------------------------------------------------- availability


def status(cfg: Config) -> tuple[bool, str]:
    """(usable, reason) — checked without loading a model or hitting the network."""
    try:
        import pyannote.audio  # noqa: F401
    except ImportError:
        return False, "pyannote.audio not installed: pip install 'pyannote.audio>=3.1'"
    if not cfg.hf_token():
        return False, (
            f"no HuggingFace token — set ${cfg.hf_token_env} or run: plaudctl speakers login"
        )
    return True, f"pyannote · {cfg.diarize_model}"


def _pipeline(cfg: Config):
    global _PIPELINE
    if _PIPELINE is not None:
        return _PIPELINE
    import torch
    from pyannote.audio import Pipeline

    pipe = Pipeline.from_pretrained(cfg.diarize_model, token=cfg.hf_token())
    if pipe is None:
        raise RuntimeError(
            f"{cfg.diarize_model} could not be loaded. The models are gated — accept the "
            "licence while signed in at "
            + " and ".join(f"https://hf.co/{m}" for m in GATED_MODELS)
            + ", then re-run."
        )
    # MPS on Apple Silicon; pyannote falls back to CPU cleanly if a kernel is missing.
    if torch.backends.mps.is_available():
        pipe.to(torch.device("mps"))
    elif torch.cuda.is_available():
        pipe.to(torch.device("cuda"))
    _PIPELINE = pipe
    return pipe


# ---------------------------------------------------------------------- vectors


def _np():
    import numpy as np

    return np


def _unit(vec):
    np = _np()
    v = np.asarray(vec, dtype="float32")
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


def cosine(a, b) -> float:
    np = _np()
    return float(np.dot(_unit(a), _unit(b)))


def _to_blob(vec) -> bytes:
    return _np().asarray(vec, dtype="float32").tobytes()


def _from_blob(blob, dim: int | None = None):
    np = _np()
    v = np.frombuffer(blob, dtype="float32")
    return v if dim is None else v[:dim]


# ---------------------------------------------------------------------- diarization


def diarization_path(cfg: Config, rec_id: str) -> Path:
    return cfg.diarization_dir / f"{rec_id}.json"


def diarize_file(cfg: Config, audio: Path, *, num_speakers: int | None = None) -> dict:
    """Run the pipeline. Returns turns plus one embedding per anonymous label.

    `exclusive_speaker_diarization` is used for the turns because it has overlapping
    speech removed, and a whisper segment must map to exactly one speaker — with
    overlaps kept, crosstalk would attribute the same words to two people.
    """
    pipe = _pipeline(cfg)
    out = pipe(str(audio), **({"num_speakers": num_speakers} if num_speakers else {}))

    # pyannote 3.x returns a bare Annotation; 4.x returns a dataclass. Support both so
    # a version bump does not silently produce a transcript with no speakers in it.
    if hasattr(out, "speaker_diarization"):
        annotation = out.exclusive_speaker_diarization or out.speaker_diarization
        order = (out.speaker_diarization or annotation).labels()
        embeddings = out.speaker_embeddings
    else:
        annotation, order, embeddings = out, out.labels(), None

    turns = [
        {"start": round(seg.start, 3), "end": round(seg.end, 3), "speaker": label}
        for seg, _, label in annotation.itertracks(yield_label=True)
    ]

    vecs: dict[str, list[float]] = {}
    if embeddings is not None:
        np = _np()
        for i, label in enumerate(order):
            if i >= len(embeddings):
                break
            v = np.asarray(embeddings[i], dtype="float32")
            # A cluster with too few samples comes back all-zero (padded). A zero
            # vector matches everything at cosine 0 and nothing usefully — storing it
            # as a voiceprint would be worse than storing nothing.
            if np.linalg.norm(v) > 0:
                vecs[label] = _unit(v).tolist()

    return {"turns": turns, "embeddings": vecs, "model": cfg.diarize_model}


def label_stats(turns: list[dict]) -> dict[str, dict]:
    stats: dict[str, dict] = {}
    for t in turns:
        st = stats.setdefault(t["speaker"], {"seconds": 0.0, "turns": 0})
        st["seconds"] += max(0.0, t["end"] - t["start"])
        st["turns"] += 1
    return stats


# ---------------------------------------------------------------------- alignment


def assign_speakers(segments: list[dict], turns: list[dict]) -> list[dict]:
    """Give every whisper segment the speaker it overlaps most.

    Whisper's segment boundaries and pyannote's turn boundaries do not agree — one
    is drawn on language, the other on voice — so a segment routinely straddles a
    handover. Max overlap is the honest reduction: the words are attributed to
    whoever spoke most of them, and a segment overlapping nothing stays unlabelled
    rather than being guessed at.
    """
    if not turns:
        return [{**s, "speaker": None} for s in segments]

    out = []
    for seg in segments:
        s0, s1 = float(seg.get("start", 0)), float(seg.get("end", 0))
        best, best_overlap = None, 0.0
        for t in turns:
            overlap = min(s1, t["end"]) - max(s0, t["start"])
            if overlap > best_overlap:
                best, best_overlap = t["speaker"], overlap
        out.append({**seg, "speaker": best})
    return out


def render_transcript(cfg: Config, store: Store, rec_id: str) -> bool:
    """Rewrite `transcripts/<id>.txt` with current names. Returns True if written.

    Derived on demand rather than baked in at diarization time, so renaming a speaker
    — or attributing a label to a different person — updates the transcript, and with
    it every summary, extraction and search chunk built from it afterwards.
    """
    json_path, txt_path = transcript_paths(cfg, rec_id)
    dpath = diarization_path(cfg, rec_id)
    if not json_path.exists() or not dpath.exists():
        return False

    row = store.get(rec_id)
    if row is None:
        return False

    result = json.loads(json_path.read_text())
    diar = json.loads(dpath.read_text())
    names = {
        r["label"]: (r["name"] or r["label"])
        for r in store.recording_speakers(rec_id)
    }
    tagged = assign_speakers(result.get("segments", []), diar.get("turns", []))

    mins = (row["duration_s"] or 0) / 60
    lines = [
        f"# {row['title'] or row['filename']}",
        f"# recorded: {time.strftime('%Y-%m-%d %H:%M', time.localtime(row['started_at']))}",
        f"# duration: {mins:.1f} min | model: {row['transcribe_model'] or ''}",
        f"# speakers: {', '.join(sorted(set(names.values()))) or 'unidentified'}",
        "",
    ]
    # Consecutive segments from one speaker get the name once. A name on every line
    # is noise to a reader and, more importantly, dilutes the transcript the language
    # model reads downstream.
    previous = object()
    for seg in tagged:
        who = names.get(seg.get("speaker") or "", seg.get("speaker"))
        stamp = _format_ts(seg["start"])
        text = seg["text"].strip()
        if who and who != previous:
            lines.append(f"[{stamp}] {who}: {text}")
        else:
            lines.append(f"[{stamp}] {text}")
        previous = who
    txt_path.write_text("\n".join(lines) + "\n")
    return True


# ---------------------------------------------------------------------- voiceprints


def rebuild_voiceprint(store: Store, speaker_id: int) -> int:
    """Recompute a person's voiceprint from every recording you confirmed was them.

    Weighted by how long they spoke: a thirty-second cameo should not move an identity
    as far as an hour of conversation. Returns how many recordings it was built from,
    zero meaning the person now has no voiceprint — which is the correct state after
    you take back the only attribution they had.
    """
    np = _np()
    rows = store.confirmed_embeddings(speaker_id)
    vectors, weights = [], []
    for r in rows:
        if not r["embedding"]:
            continue
        vectors.append(_unit(_from_blob(r["embedding"], r["embedding_dim"])))
        weights.append(max(1.0, float(r["seconds"] or 1.0)))

    if not vectors:
        store.update_speaker(
            speaker_id, voiceprint=None, voiceprint_dim=None, voiceprint_n=0
        )
        return 0

    stacked = np.vstack(vectors)
    mean = _unit(np.average(stacked, axis=0, weights=np.asarray(weights)))
    store.update_speaker(
        speaker_id,
        voiceprint=_to_blob(mean),
        voiceprint_dim=int(mean.shape[0]),
        voiceprint_n=len(vectors),
    )
    return len(vectors)


def match(store: Store, vector, *, threshold: float) -> tuple[int | None, float]:
    """Best known voice for this embedding, or (None, best_score) if none clears.

    The score is returned either way, so the console can show a near miss ("0.61 —
    probably Herry?") instead of silently offering nothing.
    """
    best_id, best_score = None, 0.0
    for sp in store.speakers():
        if not sp["voiceprint"]:
            continue
        score = cosine(vector, _from_blob(sp["voiceprint"], sp["voiceprint_dim"]))
        if score > best_score:
            best_id, best_score = sp["id"], score
    return (best_id if best_score >= threshold else None), best_score


# ---------------------------------------------------------------------- driver


def run(
    cfg: Config,
    store: Store,
    *,
    limit: int | None = None,
    force: bool = False,
    num_speakers: int | None = None,
) -> dict:
    cfg.ensure_dirs()
    ok, why = status(cfg)
    if not ok:
        raise RuntimeError(f"diarization unavailable — {why}")

    rows = (
        [r for r in store.visible() if r["transcript_path"] and r["audio_path"]]
        if force
        else store.needing_diarization()
    )
    rows = [
        r for r in rows
        if (r["duration_s"] or 0) >= cfg.diarize_min_seconds
        and r["audio_path"] and Path(r["audio_path"]).exists()
    ]
    if limit:
        rows = rows[:limit]

    stats = {"done": 0, "failed": 0, "labels": 0, "matched": 0}
    print(f"  {len(rows)} to diarize · {why}")

    for i, row in enumerate(rows, 1):
        print(f"  [{i}/{len(rows)}] {(row['title'] or row['filename'])[:60]} ...", flush=True)
        t0 = time.time()
        try:
            result = diarize_file(cfg, Path(row["audio_path"]), num_speakers=num_speakers)
            diarization_path(cfg, row["id"]).write_text(json.dumps(result, indent=1))

            counts = label_stats(result["turns"])
            store.set_recording_speakers(
                row["id"],
                [
                    {
                        "label": label,
                        "seconds": st["seconds"],
                        "turns": st["turns"],
                        "embedding": _to_blob(result["embeddings"][label])
                        if label in result["embeddings"] else None,
                        "embedding_dim": len(result["embeddings"].get(label, [])) or None,
                    }
                    for label, st in counts.items()
                ],
            )

            # Attribute what we recognise, but never over a human's decision.
            matched = 0
            for rs in store.recording_speakers(row["id"]):
                if rs["source"] == "human" or not rs["embedding"]:
                    continue
                sid, score = match(
                    store,
                    _from_blob(rs["embedding"], rs["embedding_dim"]),
                    threshold=cfg.speaker_match_threshold,
                )
                if sid:
                    store.attribute(row["id"], rs["label"], sid, source="voiceprint",
                                    confidence=score)
                    matched += 1

            render_transcript(cfg, store, row["id"])
            store.update(
                row["id"],
                diarized_at=int(time.time()),
                diarize_model=cfg.diarize_model,
            )
            stats["done"] += 1
            stats["labels"] += len(counts)
            stats["matched"] += matched
            named = f", {matched} recognised" if matched else ""
            print(f"    {len(counts)} voices{named} · {time.time() - t0:.0f}s")
        except Exception as exc:  # noqa: BLE001
            stats["failed"] += 1
            print(f"    [fail] {exc}")

    return stats


def confirm(cfg: Config, store: Store, rec_id: str, label: str,
            speaker_id: int | None) -> dict:
    """A human says who this voice is. The one write that teaches the archive.

    Three things follow from it, and they have to happen together or the archive
    disagrees with itself: the attribution is recorded as human-decided, the person's
    voiceprint is rebuilt to include (or exclude) this recording, and every transcript
    naming either the old or the new person is re-rendered.

    Passing `speaker_id=None` un-names the label — used when the machine matched the
    wrong person, which must also *remove* this recording from that person's
    voiceprint rather than leaving the mistake baked into their identity.
    """
    existing = {r["label"]: r for r in store.recording_speakers(rec_id)}
    if label not in existing:
        raise KeyError(label)
    previous = existing[label]["speaker_id"]

    store.attribute(
        rec_id, label, speaker_id,
        source="human" if speaker_id else "unassigned",
        confidence=None,
    )

    touched = {sid for sid in (previous, speaker_id) if sid}
    rebuilt = {}
    for sid in touched:
        rebuilt[sid] = rebuild_voiceprint(store, sid)

    # Re-render every transcript whose names could have moved: this one always, plus
    # anything else attributed to a speaker whose voiceprint just changed — their name
    # did not change, but a machine match made under an old voiceprint might be wrong
    # now. Only this recording is re-rendered; re-matching the rest is deliberately a
    # separate, explicit act (`plaudctl speakers rematch`) rather than a side effect.
    render_transcript(cfg, store, rec_id)
    return {"label": label, "speaker_id": speaker_id, "voiceprints": rebuilt}


def rematch(cfg: Config, store: Store, *, threshold: float | None = None) -> dict:
    """Re-run voiceprint matching over every unnamed label in the archive.

    The natural thing to do after naming somebody for the first time: the voiceprint
    that did not exist when those recordings were diarized exists now. Human
    attributions are never touched, and every match is still only a proposal you can
    take back.
    """
    thr = cfg.speaker_match_threshold if threshold is None else threshold
    stats = {"considered": 0, "matched": 0, "recordings": set()}
    for rs in store.unnamed_labels():
        if not rs["embedding"]:
            continue
        stats["considered"] += 1
        sid, score = match(store, _from_blob(rs["embedding"], rs["embedding_dim"]),
                           threshold=thr)
        if sid:
            store.attribute(rs["recording_id"], rs["label"], sid,
                            source="voiceprint", confidence=score)
            stats["matched"] += 1
            stats["recordings"].add(rs["recording_id"])
    for rec_id in stats["recordings"]:
        render_transcript(cfg, store, rec_id)
    stats["recordings"] = len(stats["recordings"])
    return stats
