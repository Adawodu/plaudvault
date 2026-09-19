"""Neighbour expansion: enough text to answer from, without loosening the citation.

A 1200-character chunk is sized so a hit points at a findable moment. That is a
different job from giving a model enough to answer with, and the gap was being closed
by hand with a second `get_transcript` call and guessed bounds.

The rule these tests hold down: the hit's own text never changes, because it is the
passage sitting at the cited timestamp. Context is a separate field with its own span,
so a client quoting the passage cannot attribute a neighbour's words to `at`.
"""

from __future__ import annotations

import time

import numpy as np

from plaudvault import search
from plaudvault.store import Store

T0 = int(time.mktime((2026, 3, 10, 9, 0, 0, 0, 0, -1)))


def _seed(db, texts, rid="r1"):
    st = Store(db)
    st.db.execute("INSERT INTO recordings (id, filename, started_at, duration_s) VALUES (?,?,?,?)",
                  (rid, f"{rid}.mp3", T0, 600.0))
    st.db.commit()
    chunks = [{"start_ms": i * 60_000, "text": t} for i, t in enumerate(texts)]
    st.set_chunks(rid, chunks, np.zeros((len(texts), 8), dtype="float32"), model="m")
    return st


# ------------------------------------------------------------------ stitching

def test_the_overlap_between_adjacent_chunks_is_not_repeated():
    """Chunks re-seed from the tail of the one before. Left in, a three-chunk window
    says the same sentence twice and a model can read that as emphasis."""
    tail = "and the thing I keep coming back to is the reimbursement pathway " * 2
    left = "We opened with the clinical results. " + tail
    right = tail + " So that is where Thursday's call has to land."
    got = search._stitch(left, right)
    assert got.count("reimbursement pathway") == 2       # the tail appears once, not twice
    assert got.startswith("We opened with the clinical results.")
    assert got.endswith("So that is where Thursday's call has to land.")


def test_chunks_with_no_shared_overlap_are_joined_not_merged():
    """An index built under a different OVERLAP_CHARS has no run to find. Repetition
    is a cosmetic failure; deleting words that were actually said is not."""
    got = search._stitch("First half of it.", "Second half of it.")
    assert got == "First half of it. Second half of it."


def test_a_short_coincidental_match_does_not_eat_text():
    """Two chunks both ending and starting with ' the ' must not be spliced there."""
    left, right = "we talked about the", "the board call went long"
    got = search._stitch(left, right)
    assert "board call went long" in got and got.startswith("we talked about")


def test_stitching_an_empty_side_is_the_other_side():
    assert search._stitch("", "only this") == "only this"
    assert search._stitch("only this", "") == "only this"


# --------------------------------------------------------------------- window

def test_a_window_gathers_the_neighbours_in_reading_order(tmp_path):
    st = _seed(tmp_path / "m.sqlite", ["alpha", "bravo", "charlie", "delta", "echo"])
    rows = st.chunk_window("r1", 2, model="m", before=1, after=1)
    assert [r["text"] for r in rows] == ["bravo", "charlie", "delta"]


def test_a_window_at_the_edge_is_short_rather_than_padded(tmp_path):
    st = _seed(tmp_path / "m.sqlite", ["alpha", "bravo", "charlie"])
    assert [r["text"] for r in st.chunk_window("r1", 0, model="m", before=2, after=1)] \
        == ["alpha", "bravo"]
    assert [r["text"] for r in st.chunk_window("r1", 2, model="m", before=1, after=2)] \
        == ["bravo", "charlie"]


def test_a_window_never_crosses_into_another_recording(tmp_path):
    db = tmp_path / "m.sqlite"
    st = _seed(db, ["alpha", "bravo"], rid="r1")
    st.db.execute("INSERT INTO recordings (id, filename, started_at, duration_s) VALUES (?,?,?,?)",
                  ("r2", "r2.mp3", T0, 600.0))
    st.db.commit()
    st.set_chunks("r2", [{"start_ms": 0, "text": "other conversation entirely"}],
                  np.zeros((1, 8), dtype="float32"), model="m")
    rows = st.chunk_window("r1", 1, model="m", before=1, after=1)
    assert [r["text"] for r in rows] == ["alpha", "bravo"]


def test_a_window_is_scoped_to_the_embedding_model(tmp_path):
    """Chunks from another model are a different segmentation of the same speech;
    mixing them would stitch text that never sat next to itself."""
    st = _seed(tmp_path / "m.sqlite", ["alpha", "bravo", "charlie"])
    assert st.chunk_window("r1", 1, model="other", before=1, after=1) == []


# --------------------------------------------------------------------- expand

def test_the_hit_text_is_untouched_and_context_is_a_separate_field(tmp_path):
    """The whole citation contract in one assertion."""
    st = _seed(tmp_path / "m.sqlite", ["alpha", "bravo", "charlie"])
    hit = {"recording_id": "r1", "idx": 1, "text": "bravo", "at": "00:01:00"}
    got = search.expand(st, hit, model="m", before=1, after=1)
    assert got["text"] == "bravo"
    assert got["at"] == "00:01:00"
    assert got["context"] == "alpha bravo charlie"
    assert got["context_from"] == "00:00:00" and got["context_to"] == "00:02:00"
    assert got["context_chunks"] == 3


def test_asking_for_no_context_returns_the_hit_unchanged(tmp_path):
    """`context=0` has to be exactly today's behaviour, byte for byte — every existing
    caller depends on it and no baseline is worth anything if it drifted."""
    st = _seed(tmp_path / "m.sqlite", ["alpha", "bravo", "charlie"])
    hit = {"recording_id": "r1", "idx": 1, "text": "bravo", "at": "00:01:00"}
    assert search.expand(st, hit, model="m", before=0, after=0) == hit


def test_a_hit_with_no_index_is_returned_unchanged(tmp_path):
    st = _seed(tmp_path / "m.sqlite", ["alpha"])
    hit = {"recording_id": "r1", "idx": None, "text": "alpha"}
    assert search.expand(st, hit, model="m") == hit


def test_a_recording_of_one_chunk_yields_context_equal_to_the_hit(tmp_path):
    """Nothing to add. The caller drops the field rather than showing the same text
    twice under two names."""
    st = _seed(tmp_path / "m.sqlite", ["alpha only"])
    hit = {"recording_id": "r1", "idx": 0, "text": "alpha only"}
    got = search.expand(st, hit, model="m", before=2, after=2)
    assert got["context"] == "alpha only"
