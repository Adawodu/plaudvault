"""The identity half of diarization, which is where it can go quietly wrong.

Diarization itself is pyannote's problem and is not tested here — it needs gated
models and real audio. What *is* tested is everything plaudvault builds on top of it,
because those are the rules that make an identity trustworthy rather than merely
present, and every one of them fails silently:

  - a machine guess must never feed a voiceprint (or one bad match becomes an identity)
  - a human attribution must survive a re-run (or naming somebody is not durable)
  - taking back an attribution must remove it from the voiceprint (or the mistake stays)
  - a rename must reach every transcript (or the archive disagrees with itself)

Embeddings are synthetic: two random unit vectors standing in for two people, with
same-speaker variation small enough to land where pyannote's actually do (~0.95
cosine within a speaker, ~0.0 between two random ones).
"""

from __future__ import annotations

import json
import time

import numpy as np
import pytest

from plaudvault import diarize
from plaudvault.transcribe import transcript_paths

DIM = 192


@pytest.fixture
def rng():
    return np.random.default_rng(7)


def _person(rng):
    v = rng.normal(size=DIM)
    return v / np.linalg.norm(v)


def _sample(rng, base, sd=0.02):
    """One recording's worth of a person's voice: the same speaker, not the same vector."""
    v = base + rng.normal(scale=sd, size=base.shape)
    return v / np.linalg.norm(v)


class _Remote:
    """The shape `Store.upsert_remote` wants, without importing the API client."""

    def __init__(self, rid: str, when: int):
        self.id = rid
        self.filename = f"{rid}.mp3"
        self.start_time_ms = when * 1000
        self.duration_s = 600.0
        self.serial_number = "sn"
        self.file_md5 = ""
        self.filesize = 1
        self.raw = {}


SEGMENTS = [
    {"start": 0, "end": 30, "text": "I'll send the deck Friday."},
    {"start": 31, "end": 60, "text": "Perfect, I'll review it over the weekend."},
    {"start": 61, "end": 90, "text": "And I'll loop in the clinic contact."},
]


def add_recording(cfg, store, rid: str, when: int) -> None:
    store.upsert_remote(_Remote(rid, when))
    json_path, txt_path = transcript_paths(cfg, rid)
    json_path.write_text(json.dumps({"segments": SEGMENTS, "text": "", "language": "en"}))
    txt_path.write_text("# placeholder\n")
    store.update(
        rid,
        transcript_path=str(txt_path),
        transcribed_at=int(time.time()),
        transcribe_model="test",
        audio_path="/dev/null",
    )


def add_diarization(cfg, store, rid: str, mapping: dict) -> None:
    """`mapping` is label -> (embedding, [(start, end), ...]) — what a run would leave."""
    turns = [
        {"start": a, "end": b, "speaker": label}
        for label, (_, spans) in mapping.items()
        for a, b in spans
    ]
    diarize.diarization_path(cfg, rid).write_text(
        json.dumps(
            {
                "turns": turns,
                "embeddings": {lb: v.tolist() for lb, (v, _) in mapping.items()},
                "model": "test",
            }
        )
    )
    store.set_recording_speakers(
        rid,
        [
            {
                "label": lb,
                "seconds": sum(b - a for a, b in spans),
                "turns": len(spans),
                "embedding": diarize._to_blob(v),
                "embedding_dim": len(v),
            }
            for lb, (v, spans) in mapping.items()
        ],
    )


@pytest.fixture
def archive(tmp_path, monkeypatch, rng):
    """Three recordings of the same two people, diarized, nobody named yet.

    The labels deliberately swap between rec1 and rec2 — SPEAKER_00 is Bayo in one and
    Herry in the other — because that is exactly what real diarization does, and it is
    the reason anonymous labels cannot be an identity on their own.
    """
    monkeypatch.setenv("PLAUDVAULT_ARCHIVE_ROOT", str(tmp_path / "archive"))
    monkeypatch.setenv("PLAUDVAULT_VAULT_ROOT", "")
    monkeypatch.setenv("PLAUDVAULT_CONFIG", str(tmp_path / "absent.toml"))

    from plaudvault.config import load
    from plaudvault.store import Store

    cfg = load()
    cfg.ensure_dirs()
    store = Store(cfg.db_path)

    bayo, herry = _person(rng), _person(rng)
    t0 = int(time.time()) - 3 * 86400

    add_recording(cfg, store, "rec1", t0)
    add_diarization(cfg, store, "rec1", {
        "SPEAKER_00": (_sample(rng, bayo), [(0, 30), (61, 90)]),
        "SPEAKER_01": (_sample(rng, herry), [(31, 60)]),
    })
    add_recording(cfg, store, "rec2", t0 + 3600)
    add_diarization(cfg, store, "rec2", {
        "SPEAKER_00": (_sample(rng, herry), [(31, 60)]),
        "SPEAKER_01": (_sample(rng, bayo), [(0, 30), (61, 90)]),
    })
    add_recording(cfg, store, "rec3", t0 + 7200)
    add_diarization(cfg, store, "rec3", {"SPEAKER_00": (_sample(rng, bayo), [(0, 90)])})

    yield cfg, store
    store.close()


def test_starts_with_every_voice_unnamed(archive):
    cfg, store = archive
    assert len(store.unnamed_labels()) == 5


def test_one_confirmation_builds_a_voiceprint(archive):
    cfg, store = archive
    bayo = store.add_speaker("Bayo", is_me=True)
    diarize.confirm(cfg, store, "rec1", "SPEAKER_00", bayo)

    row = store.speaker(bayo)
    assert row["voiceprint"] is not None
    assert row["voiceprint_n"] == 1


def test_transcript_names_the_speaker_on_change_only(archive):
    cfg, store = archive
    bayo = store.add_speaker("Bayo", is_me=True)
    diarize.confirm(cfg, store, "rec1", "SPEAKER_00", bayo)

    text = (cfg.transcript_dir / "rec1.txt").read_text()
    assert "Bayo: I'll send the deck Friday." in text
    assert "speakers: Bayo, SPEAKER_01" in text
    # Two runs of Bayo separated by the other speaker — a name on every line is noise
    # to a reader and dilutes the transcript the language model reads downstream.
    assert text.count("Bayo:") == 2


def test_naming_once_recognises_the_voice_everywhere_else(archive):
    """The whole point: you name yourself once, later recordings find you."""
    cfg, store = archive
    bayo = store.add_speaker("Bayo", is_me=True)
    diarize.confirm(cfg, store, "rec1", "SPEAKER_00", bayo)

    stats = diarize.rematch(cfg, store)
    assert stats["matched"] == 2  # rec2's SPEAKER_01 and rec3's SPEAKER_00

    labels = {r["label"]: r for r in store.recording_speakers("rec2")}
    assert labels["SPEAKER_01"]["name"] == "Bayo"
    # Drawn as a guess, never as a confirmation — an automatic attribution that looks
    # identical to a confirmed one is how a wrong identity becomes invisible.
    assert labels["SPEAKER_01"]["source"] == "voiceprint"
    assert labels["SPEAKER_00"]["speaker_id"] is None  # the other person stays unnamed


def test_machine_guesses_never_feed_the_voiceprint(archive):
    """Otherwise one bad match compounds into a drifting identity, silently."""
    cfg, store = archive
    bayo = store.add_speaker("Bayo", is_me=True)
    diarize.confirm(cfg, store, "rec1", "SPEAKER_00", bayo)
    diarize.rematch(cfg, store)

    assert store.speaker(bayo)["voiceprint_n"] == 1

    diarize.confirm(cfg, store, "rec2", "SPEAKER_01", bayo)
    assert store.speaker(bayo)["voiceprint_n"] == 2


def test_rediarizing_preserves_a_human_attribution(archive, rng):
    """A re-run must never silently un-name somebody, as triage survives a re-sync."""
    cfg, store = archive
    bayo = store.add_speaker("Bayo", is_me=True)
    diarize.confirm(cfg, store, "rec1", "SPEAKER_00", bayo)
    before = {r["label"]: (r["speaker_id"], r["source"]) for r in store.recording_speakers("rec1")}

    add_diarization(cfg, store, "rec1", {
        "SPEAKER_00": (_sample(rng, _person(rng)), [(0, 30), (61, 90)]),
        "SPEAKER_01": (_sample(rng, _person(rng)), [(31, 60)]),
    })

    after = {r["label"]: (r["speaker_id"], r["source"]) for r in store.recording_speakers("rec1")}
    assert after["SPEAKER_00"] == (bayo, "human")
    assert before == after


def test_correcting_a_wrong_match_removes_it_from_the_voiceprint(archive):
    """A correction has to reach the identity, not just the label it was shown on."""
    cfg, store = archive
    bayo = store.add_speaker("Bayo", is_me=True)
    diarize.confirm(cfg, store, "rec1", "SPEAKER_00", bayo)
    diarize.confirm(cfg, store, "rec2", "SPEAKER_01", bayo)
    assert store.speaker(bayo)["voiceprint_n"] == 2

    diarize.confirm(cfg, store, "rec2", "SPEAKER_01", None)

    assert store.speaker(bayo)["voiceprint_n"] == 1
    labels = {r["label"]: r for r in store.recording_speakers("rec2")}
    assert labels["SPEAKER_01"]["speaker_id"] is None


def test_renaming_a_person_rewrites_their_transcripts(archive):
    cfg, store = archive
    bayo = store.add_speaker("Bayo", is_me=True)
    diarize.confirm(cfg, store, "rec1", "SPEAKER_00", bayo)

    store.update_speaker(bayo, name="Bayo Dawodu")
    diarize.render_transcript(cfg, store, "rec1")

    assert "Bayo Dawodu:" in (cfg.transcript_dir / "rec1.txt").read_text()


def test_forgetting_a_person_keeps_the_diarization(archive):
    """Their labels revert to unnamed rather than vanishing, so they can be re-attributed."""
    cfg, store = archive
    bayo = store.add_speaker("Bayo", is_me=True)
    diarize.confirm(cfg, store, "rec1", "SPEAKER_00", bayo)

    store.delete_speaker(bayo)

    rows = store.recording_speakers("rec1")
    assert len(rows) == 2
    assert all(r["speaker_id"] is None for r in rows)
    assert all(r["embedding"] is not None for r in rows)


def test_a_degenerate_embedding_matches_nobody(archive):
    """pyannote pads under-sampled clusters with zeros; a zero vector must not match."""
    cfg, store = archive
    bayo = store.add_speaker("Bayo", is_me=True)
    diarize.confirm(cfg, store, "rec1", "SPEAKER_00", bayo)

    assert diarize.match(store, np.zeros(DIM), threshold=0.65) == (None, 0.0)


def test_segments_are_attributed_by_maximum_overlap():
    """Whisper's boundaries and pyannote's disagree; a segment goes to whoever spoke most."""
    segments = [
        {"start": 0, "end": 5, "text": "a"},
        {"start": 5, "end": 10, "text": "b"},
        {"start": 30, "end": 31, "text": "c"},  # overlaps no turn at all
    ]
    turns = [
        {"start": 0, "end": 4.5, "speaker": "SPEAKER_00"},
        {"start": 4.6, "end": 11, "speaker": "SPEAKER_01"},
    ]
    assert [s["speaker"] for s in diarize.assign_speakers(segments, turns)] == [
        "SPEAKER_00",
        "SPEAKER_01",
        None,  # unlabelled rather than guessed at
    ]
