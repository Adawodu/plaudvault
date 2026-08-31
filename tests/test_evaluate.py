"""The measuring instrument needs to be right before its readings mean anything.

An eval harness fails in a way that is worse than not having one: it produces a number,
the number looks like evidence, and nobody re-derives it. So the properties tested here
are the ones that would make a reading dishonest rather than merely wrong —

  - a query that never surfaces its recording must drag the mean down, not be excluded
  - a set at ceiling must announce that it can no longer detect anything
  - two runs whose corpus also moved must be flagged as unattributable
  - unverified generated queries must not silently become the headline number
"""

from __future__ import annotations

import pytest

from plaudvault import evaluate


def _q(rank, kind="direct"):
    return {"query": "q", "recording_id": "r", "recording": "R", "kind": kind,
            "rank": rank, "top_score": 0.5, "verified": True}


# ------------------------------------------------------------------- scoring


def test_a_perfect_run_scores_one():
    m = evaluate._score([_q(1), _q(1), _q(1)])
    assert m["recall@1"] == 1.0
    assert m["mrr"] == 1.0


def test_a_query_that_never_surfaces_counts_against_the_mean():
    """Averaging only over found queries would hide exactly what this exists to find."""
    m = evaluate._score([_q(1), _q(None)])

    assert m["recall@1"] == 0.5
    assert m["recall@10"] == 0.5
    assert m["mrr"] == 0.5  # (1/1 + 0) / 2, not 1.0 over the single hit


def test_recall_is_cumulative_across_depths():
    m = evaluate._score([_q(1), _q(4), _q(9), _q(None)])

    assert m["recall@1"] == 0.25
    assert m["recall@3"] == 0.25
    assert m["recall@5"] == 0.5
    assert m["recall@10"] == 0.75


def test_mrr_rewards_rank_one_over_rank_ten():
    assert evaluate._score([_q(1)])["mrr"] > evaluate._score([_q(10)])["mrr"]


def test_an_empty_set_does_not_divide_by_zero():
    assert evaluate._score([])["queries"] == 0


# ---------------------------------------------------------------- comparison


def _result(recall1, chunks=1000, model="nomic-embed-text", prefix="search_query: "):
    return {
        "config": {"embed_model": model, "chunk_chars": 1200, "overlap_chars": 200,
                   "query_prefix": prefix, "chunks": chunks, "corpus_recordings": 50},
        "metrics": {"recall@1": recall1, "recall@5": recall1, "mrr": recall1,
                    "queries": 10, "not_found": 0},
    }


def test_a_config_change_on_a_stable_corpus_is_attributable():
    cmp = evaluate.compare(_result(0.5), _result(0.7, prefix=None))

    assert cmp["changed_config"] == {"query_prefix": ("search_query: ", None)}
    assert cmp["deltas"]["recall@1"] == pytest.approx(0.2)
    assert not cmp["confounded"]


def test_a_config_change_plus_a_corpus_change_is_flagged_as_confounded():
    """Two things moved, so the delta cannot be attributed to either — say so."""
    cmp = evaluate.compare(_result(0.5), _result(0.7, chunks=2000, prefix=None))

    assert cmp["corpus_moved"]
    assert cmp["confounded"]


def test_a_corpus_change_alone_is_not_confounded():
    """Nothing was being attributed, so there is nothing to confound."""
    cmp = evaluate.compare(_result(0.5), _result(0.7, chunks=2000))

    assert cmp["corpus_moved"]
    assert not cmp["confounded"]
    assert cmp["changed_config"] == {}


# ---------------------------------------------------------------- the queries


def test_generated_questions_survive_a_code_fence():
    raw = '```json\n["Why did we drop the second vendor?", "Was I underpaid then?"]\n```'
    assert len(evaluate._parse_questions(raw)) == 2


def test_a_non_answer_yields_no_questions():
    assert evaluate._parse_questions("I cannot write questions about this.") == []
    assert evaluate._parse_questions("") == []


def test_trivially_short_questions_are_discarded():
    """"What?" is not a query, and would score as a free pass or a free failure."""
    assert evaluate._parse_questions('["What?", "ok", "Why did we drop the vendor?"]') == [
        "Why did we drop the vendor?"
    ]


# ------------------------------------------------------------------ the golden set


def test_the_golden_set_lives_in_the_archive_not_the_repo(tmp_path, monkeypatch):
    """It is as personal as the recordings it points at, and this repo is public."""
    monkeypatch.setenv("PLAUDVAULT_ARCHIVE_ROOT", str(tmp_path / "archive"))
    monkeypatch.setenv("PLAUDVAULT_CONFIG", str(tmp_path / "absent.toml"))
    from plaudvault.config import load

    cfg = load()
    assert evaluate.golden_path(cfg).is_relative_to(cfg.archive_root)


def test_a_golden_set_round_trips_with_comments_and_blank_lines(tmp_path, monkeypatch):
    """The file is hand-editable — curating it is the point — so it must tolerate that."""
    monkeypatch.setenv("PLAUDVAULT_ARCHIVE_ROOT", str(tmp_path / "archive"))
    monkeypatch.setenv("PLAUDVAULT_CONFIG", str(tmp_path / "absent.toml"))
    from plaudvault.config import load

    cfg = load()
    rows = [
        {"query": "was I underpaid", "recording_id": "r1", "kind": "oblique",
         "verified": True},
        {"query": "what did the vendor quote", "recording_id": "r2", "kind": "direct",
         "verified": False},
    ]
    evaluate.save_golden(cfg, rows)

    path = evaluate.golden_path(cfg)
    path.write_text("# a note to self\n\n" + path.read_text())

    assert evaluate.load_golden(cfg) == rows
