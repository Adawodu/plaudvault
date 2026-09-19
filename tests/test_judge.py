"""The labelled set, and what it is allowed to claim.

Two rankers were built for the action budget and neither could be validated, because
the archive's own history is seven accepted actions against 975 dropped — 539 of them
in bursts of ten or more, which is a person clearing a board rather than judging items.
This module makes the missing evidence, and these tests hold down the properties that
decide whether it is worth anything: labels that survive a change of ranker, an
unlabelled item that counts as nothing rather than as a mistake, and recall reported
beside precision so "keep nothing" cannot look perfect.
"""

from __future__ import annotations

import time

from plaudvault import judge
from plaudvault.store import Store

T0 = int(time.mktime((2026, 3, 10, 9, 0, 0, 0, 0, -1)))


class _Cfg:
    def __init__(self, root):
        self.archive_root = root


def _store(db, per_recording=6, recordings=("r1", "r2")):
    st = Store(db)
    for rid in recordings:
        st.db.execute("INSERT INTO recordings (id, filename, started_at, duration_s) VALUES (?,?,?,?)",
                      (rid, f"{rid}.mp3", T0, 600.0))
        st.db.execute("INSERT INTO conversation_kinds (recording_id, kind, source, decided_at)"
                      " VALUES (?,?,?,?)", (rid, "working", "model", T0))
        for i in range(per_recording):
            st.db.execute("INSERT INTO actions (recording_id, text, kind, status, created_at)"
                          " VALUES (?,?,?,?,?)",
                          (rid, f"{rid} action {i}", "commitment", "proposed", T0))
    st.db.commit()
    return st


# ------------------------------------------------------------------ the set

def test_verdicts_round_trip(tmp_path):
    cfg = _Cfg(tmp_path)
    judge.record(cfg, [{"action_id": 1, "recording_id": "r1", "text": "x", "verdict": "keep"},
                       {"action_id": 2, "recording_id": "r1", "text": "y", "verdict": "drop"}])
    assert judge.verdicts(cfg) == {1: "keep", 2: "drop"}


def test_the_set_is_append_only_and_the_last_verdict_wins(tmp_path):
    """Changing your mind is a new line, not an edit — every history here works this
    way, so a judgement can be traced rather than just observed."""
    cfg = _Cfg(tmp_path)
    judge.record(cfg, [{"action_id": 1, "verdict": "drop"}])
    judge.record(cfg, [{"action_id": 1, "verdict": "keep"}])
    assert len(judge.load_judged(cfg)) == 2
    assert judge.verdicts(cfg) == {1: "keep"}


def test_the_set_lives_in_the_archive_not_the_repository(tmp_path):
    """A commitment is as personal as the recording it came from."""
    cfg = _Cfg(tmp_path)
    assert judge.judged_path(cfg) == tmp_path / "eval" / "actions.jsonl"


def test_an_absent_set_is_empty_not_an_error(tmp_path):
    assert judge.load_judged(_Cfg(tmp_path)) == []
    assert judge.verdicts(_Cfg(tmp_path)) == {}


# ------------------------------------------------------------------ sampling

def test_candidates_are_never_shown_in_extraction_order(tmp_path):
    """Extraction order is one of the things being measured. Showing it to the judge
    anchors the verdict on the baseline."""
    st = _store(tmp_path / "m.sqlite", per_recording=12, recordings=("r1",))
    pools = judge.sample(_Cfg(tmp_path), st, recordings=1, seed=3)
    ids = [a["id"] for a in pools[0]["candidates"]]
    assert ids != sorted(ids)


def test_sampling_is_reproducible_for_a_seed(tmp_path):
    st = _store(tmp_path / "m.sqlite", per_recording=8)
    cfg = _Cfg(tmp_path)
    a = judge.sample(cfg, st, recordings=2, seed=11)
    b = judge.sample(cfg, st, recordings=2, seed=11)
    assert [p["recording_id"] for p in a] == [p["recording_id"] for p in b]


def test_a_fully_labelled_recording_is_not_offered_again(tmp_path):
    st = _store(tmp_path / "m.sqlite", per_recording=4, recordings=("r1",))
    cfg = _Cfg(tmp_path)
    ids = [a["id"] for a in st.actions(recording_id="r1")]
    judge.record(cfg, [{"action_id": i, "verdict": "drop"} for i in ids])
    assert judge.sample(cfg, st, recordings=4) == []


def test_a_recording_too_short_to_force_a_choice_is_skipped(tmp_path):
    """A pool the budget does not cut teaches nothing about cutting."""
    st = _store(tmp_path / "m.sqlite", per_recording=2, recordings=("r1",))
    assert judge.sample(_Cfg(tmp_path), st, recordings=4) == []


# ------------------------------------------------------------------ the number

def test_an_unlabelled_pick_counts_as_nothing_not_as_a_miss():
    """Otherwise a half-labelled set makes a working ranker look broken."""
    truth = {1: "keep", 2: "drop"}
    got = judge.precision([1, 2, 99], truth)
    assert got == {"picked": 3, "labelled": 2, "correct": 1, "precision": 0.5}


def test_precision_is_none_rather_than_zero_when_nothing_is_labelled():
    """Zero would be a claim. None is the absence of one."""
    assert judge.precision([7, 8], {})["precision"] is None


def test_recall_is_reported_so_keeping_nothing_cannot_look_perfect():
    truth = {1: "keep", 2: "keep", 3: "drop"}
    empty = judge.score_board([], [1, 2, 3], truth)
    assert empty["precision"] is None and empty["recall"] == 0.0
    both = judge.score_board([1, 2], [1, 2, 3], truth)
    assert both["precision"] == 1.0 and both["recall"] == 1.0


def test_a_board_scores_against_its_own_pool():
    truth = {1: "keep", 2: "drop", 3: "keep", 4: "drop"}
    got = judge.score_board([1, 2], [1, 2, 3, 4], truth)
    assert got["correct"] == 1 and got["keepers_in_pool"] == 2 and got["recall"] == 0.5


def test_the_sitting_is_bounded_not_just_the_sample(tmp_path):
    """A set nobody finishes is worth less than a smaller one somebody does — and an
    abandoned labelling session is how the bulk-drop history happened."""
    st = _store(tmp_path / "m.sqlite", per_recording=20,
                recordings=("r1", "r2", "r3", "r4", "r5"))
    pools = judge.sample(_Cfg(tmp_path), st, recordings=5, max_items=25)
    assert sum(len(p["candidates"]) for p in pools) <= 40   # one pool may exceed on its own
    assert len(pools) <= 2


def test_a_single_pool_larger_than_the_cap_is_still_offered(tmp_path):
    """Otherwise the recordings that most need judging — the long ones — are the
    only ones never sampled."""
    st = _store(tmp_path / "m.sqlite", per_recording=30, recordings=("r1",))
    pools = judge.sample(_Cfg(tmp_path), st, recordings=1, max_items=10)
    assert len(pools) == 1 and len(pools[0]["candidates"]) == 30
