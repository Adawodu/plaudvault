"""Spending a budget of three on a list of fourteen.

A rubric was built for this first and measured against the only ground truth the
archive holds — the seven actions its owner kept. It placed one of them inside its
budget, and scored 0.382 on mean position where extraction order scored 0.449 and
random 0.487. The lesson is in `select.py`'s docstring: what separates a kept action
from a dropped one is not in the sentence, so a sentence-scorer cannot find it.

What these tests hold down is everything around the model's judgement — the parts that
must behave identically whether it judges well or badly. Nothing is invented, nothing is
deleted, an empty answer is respected, and a failed one changes nothing.
"""

from __future__ import annotations

import pytest

from plaudvault import select

CANDIDATES = [
    {"text": "Send the deck to the clinical lead", "quote": "I'll send the deck Friday"},
    {"text": "Discuss the roadmap", "quote": "let's talk about the roadmap"},
    {"text": "Make this a reality", "quote": "we're going to make this a reality"},
    {"text": "Book the venue", "quote": "I'll book the venue tomorrow"},
]


def _cfg_returning(raw):
    return raw


# --------------------------------------------------------------------- parsing

def test_a_clean_choice_is_read():
    got = select._parse('{"keep": [{"n": 1, "why": "stated commitment"}]}',
                        n_candidates=4, budget=3)
    assert got == [{"index": 0, "why": "stated commitment"}]


def test_an_empty_choice_is_a_real_answer_not_a_failure():
    """`[]` means none of these are real, which is frequently correct. Collapsing it
    into 'the model failed' would put twelve items back on the board."""
    assert select._parse('{"keep": []}', n_candidates=4, budget=3) == []


def test_an_unparseable_reply_is_not_an_empty_choice():
    """The opposite mistake: treating a failed parse as 'keep nothing' would hide a
    whole conversation's work behind a bad JSON day."""
    for raw in ("I think candidates 1 and 4 are good", "", "```\nnope\n```"):
        assert select._parse(raw, n_candidates=4, budget=3) is None


def test_a_fenced_reply_is_read():
    got = select._parse('```json\n{"keep": [{"n": 2}]}\n```', n_candidates=4, budget=3)
    assert got == [{"index": 1, "why": ""}]


def test_bare_numbers_are_accepted():
    """Small models drop the object wrapper. The choice is still legible."""
    assert select._parse('{"keep": [1, 3]}', n_candidates=4, budget=3) == [
        {"index": 0, "why": ""}, {"index": 2, "why": ""}]


def test_an_index_outside_the_list_is_dropped_not_clamped():
    """Clamping would silently promote whatever sits at the edge of the list — an
    action nobody chose, arriving with a reason that belongs to a different one."""
    assert select._parse('{"keep": [{"n": 99}, {"n": 0}, {"n": 2}]}',
                         n_candidates=4, budget=3) == [{"index": 1, "why": ""}]


def test_a_repeated_index_is_counted_once():
    assert select._parse('{"keep": [1, 1, 1]}', n_candidates=4, budget=3) == [
        {"index": 0, "why": ""}]


def test_going_over_budget_is_trimmed_from_the_end():
    """The model's order is its preference, so the surplus comes off the bottom."""
    got = select._parse('{"keep": [4, 3, 2, 1]}', n_candidates=4, budget=2)
    assert [g["index"] for g in got] == [3, 2]


# ---------------------------------------------------------------- the decision

def test_nothing_is_discarded_only_moved(monkeypatch):
    monkeypatch.setattr(select, "_generate", lambda *a, **k: '{"keep": [{"n": 1, "why": "real"}]}')
    got = select.choose(object(), CANDIDATES, summary="a work call", kind="working", budget=1)
    assert len(got["kept"]) == 1
    assert len(got["overflow"]) == 3
    assert len(got["kept"]) + len(got["overflow"]) == len(CANDIDATES)
    assert got["kept"][0]["text"] == "Send the deck to the clinical lead"
    assert got["kept"][0]["selection_note"] == "real"


def test_a_failed_selection_keeps_everything_and_says_so(monkeypatch):
    """A conversation's whole output must not vanish because a parse failed. The run
    reports it rather than showing a board that looks complete."""
    monkeypatch.setattr(select, "_generate", lambda *a, **k: "sorry, I can't")
    got = select.choose(object(), CANDIDATES, summary="", kind="working", budget=2)
    assert got["decided"] is False
    assert len(got["kept"]) == len(CANDIDATES)
    assert got["overflow"] == []


def test_a_model_that_keeps_nothing_is_obeyed(monkeypatch):
    """Permission to return none is the point. A conversation with no commitments in
    it should produce no actions, not the least-bad three."""
    monkeypatch.setattr(select, "_generate", lambda *a, **k: '{"keep": []}')
    got = select.choose(object(), CANDIDATES, summary="", kind="personal", budget=2)
    assert got["kept"] == []
    assert len(got["overflow"]) == 4
    assert got["decided"] is True


def test_a_list_already_inside_its_budget_is_not_sent_to_the_model(monkeypatch):
    """This step exists to cut a list down, not to re-litigate a short one — and a
    call that can only lose you something is a call not worth making."""
    calls = []
    monkeypatch.setattr(select, "_generate", lambda *a, **k: calls.append(1) or '{"keep": []}')
    got = select.choose(object(), CANDIDATES[:2], summary="", kind="working", budget=3)
    assert calls == []
    assert len(got["kept"]) == 2 and got["overflow"] == []


def test_a_budget_of_zero_moves_everything_below_the_line(monkeypatch):
    """`devotional` never reaches here through `extract.run`, but the contract holds
    on its own: budget 0 means nothing on the board, and still nothing deleted."""
    calls = []
    monkeypatch.setattr(select, "_generate", lambda *a, **k: calls.append(1) or '{"keep": []}')
    got = select.choose(object(), CANDIDATES, summary="", kind="devotional", budget=0)
    assert calls == []
    assert got["kept"] == [] and len(got["overflow"]) == 4


def test_no_candidates_is_not_a_model_call(monkeypatch):
    calls = []
    monkeypatch.setattr(select, "_generate", lambda *a, **k: calls.append(1) or "{}")
    got = select.choose(object(), [], summary="", kind="working", budget=3)
    assert calls == [] and got == {"kept": [], "overflow": [], "decided": True}


def test_the_tier_reaches_the_provider_call(monkeypatch):
    seen = []
    monkeypatch.setattr(select, "_generate", lambda cfg, p, *, tier=None, **k: (
        seen.append(tier) or '{"keep": [1]}'))
    select.choose(object(), CANDIDATES, summary="s", kind="working", budget=1, tier="local")
    assert seen == ["local"]


def test_the_model_sees_the_quote_not_only_the_paraphrase():
    """The paraphrase is normalised into an imperative, which flattens the speech act.
    The quote is the evidence, and the evidence is what the choice rests on."""
    prompt = select._prompt(summary="s", kind="working", budget=3, candidates=CANDIDATES)
    assert "I'll send the deck Friday" in prompt
    assert "AT MOST 3" in prompt


def test_the_prompt_gives_permission_to_return_nothing():
    """The single line that counteracts a leading question asked once per chunk."""
    prompt = select._prompt(summary="s", kind="working", budget=3, candidates=CANDIDATES)
    assert '{"keep": []}' in prompt
    assert "correct to return none" in prompt.lower()


# ------------------------------------------------- what the store does with it

import time  # noqa: E402

from plaudvault.store import Store  # noqa: E402

T0 = int(time.mktime((2026, 3, 10, 9, 0, 0, 0, 0, -1)))


class _RunCfg:
    extract_suggestions = False
    llm_label = "test"

    def __init__(self, tmp_path):
        self.summary_dir = tmp_path / "summaries"


def _seeded(tmp_path, kind="working"):
    st = Store(tmp_path / "m.sqlite")
    st.db.execute("INSERT INTO recordings (id, filename, started_at, duration_s,"
                  " transcript_path) VALUES (?,?,?,?,?)",
                  ("r1", "r1.mp3", T0, 900.0, str(tmp_path / "r1.txt")))
    st.db.commit()
    st.set_kind("r1", kind=kind, source="model")
    return st


def test_extraction_stores_the_cut_rather_than_discarding_it(tmp_path, monkeypatch):
    from plaudvault import extract

    st = _seeded(tmp_path)
    monkeypatch.setattr(extract, "available", lambda cfg: (True, "ok"))
    monkeypatch.setattr(extract, "read_transcript",
                        lambda cfg, rid: "[00:00:00] I'll send the deck Friday.\n")
    monkeypatch.setattr(extract, "extract_from_text", lambda *a, **k: [
        {"text": "Send the deck", "quote": "I'll send the deck Friday", "owner": "Bayo",
         "kind": "commitment", "at_ms": 0},
        {"text": "Discuss the roadmap", "quote": "let's talk roadmap", "owner": "",
         "kind": "commitment", "at_ms": 0},
        {"text": "Make this a reality", "quote": "we'll make this a reality", "owner": "",
         "kind": "commitment", "at_ms": 0},
        {"text": "Book the venue", "quote": "I'll book the venue", "owner": "Bayo",
         "kind": "commitment", "at_ms": 0},
    ])
    monkeypatch.setattr("plaudvault.select._generate",
                        lambda *a, **k: '{"keep": [{"n": 1, "why": "stated commitment"}]}')

    stats = extract.run(_RunCfg(tmp_path), st)
    assert stats["proposed"] == 1
    assert stats["overflow"] == 3
    board = st.actions(recording_id="r1", status="proposed")
    assert [a["text"] for a in board] == ["Send the deck"]
    assert board[0]["selection_note"] == "stated commitment"
    # Nothing lost: every candidate is still in the archive.
    assert len(st.actions(recording_id="r1")) == 4


def test_an_overflow_action_can_be_promoted_back(tmp_path, monkeypatch):
    """The reason a cut is safe to make: disagreeing with it costs one click."""
    st = _seeded(tmp_path)
    aid = st.add_action(recording_id="r1", text="Book the venue", status="overflow",
                        kind="commitment")
    assert st.promote_action(aid) is True
    assert st.get_action(aid)["status"] == "proposed"
    assert st.overflow_counts() == {}


def test_promoting_something_that_is_not_overflow_changes_nothing(tmp_path):
    st = _seeded(tmp_path)
    aid = st.add_action(recording_id="r1", text="x", status="accepted", kind="commitment")
    assert st.promote_action(aid) is False
    assert st.get_action(aid)["status"] == "accepted"
    assert st.promote_action(99999) is False


def test_the_cut_is_journalled_like_every_other_status_change(tmp_path):
    """History survives the board. A promotion is a decision and is traceable."""
    st = _seeded(tmp_path)
    aid = st.add_action(recording_id="r1", text="x", status="overflow", kind="commitment")
    st.promote_action(aid)
    moves = [(e["from_status"], e["to_status"]) for e in st.action_events(aid)]
    assert (None, "overflow") in moves and ("overflow", "proposed") in moves


def test_overflow_is_counted_per_recording(tmp_path):
    st = _seeded(tmp_path)
    for i in range(3):
        st.add_action(recording_id="r1", text=f"x{i}", status="overflow", kind="commitment")
    st.add_action(recording_id="r1", text="on the board", status="proposed", kind="commitment")
    assert st.overflow_counts() == {"r1": 3}


# --------------------------------------------- choosing with a bigger model

def test_selection_can_run_on_a_larger_model_while_extraction_stays_local(tmp_path, monkeypatch):
    """The split that makes on-demand cloud worth it: extraction is ~15 calls per
    recording and wants recall; selection is one call and is pure judgement."""
    from dataclasses import dataclass

    from plaudvault import extract

    @dataclass
    class Cfg:
        extract_suggestions: bool = False
        llm_provider: str = "ollama"
        ollama_host: str = "http://127.0.0.1:11434"
        ollama_model: str = "qwen3:latest"
        openai_base_url: str = "http://127.0.0.1:1234/v1"
        openai_model: str = "x"
        cloud_model: str = "gpt-oss:120b-cloud"
        cloud_tier_scope: str = "stack"
        llm_num_ctx: int = 8192
        cloud_num_ctx: int = 131072
        summary_dir = None

        @property
        def llm_label(self):
            return f"ollama:{self.ollama_model}"

    st = _seeded(tmp_path)
    cfg = Cfg()
    cfg.summary_dir = tmp_path / "summaries"

    used = []
    monkeypatch.setattr(extract, "available", lambda cfg: (True, "ok"))
    monkeypatch.setattr(extract, "read_transcript", lambda cfg, rid: "[00:00:00] hi\n")
    monkeypatch.setattr(extract, "extract_from_text", lambda c, *a, **k: (
        used.append(("extract", c.ollama_model)) or [
            {"text": f"Action {i}", "quote": f"I'll do {i}", "owner": "Bayo",
             "kind": "commitment", "at_ms": 0} for i in range(4)]))
    monkeypatch.setattr("plaudvault.select._generate", lambda c, p, **k: (
        used.append(("select", c.ollama_model)) or '{"keep": [1]}'))

    extract.run(cfg, st, cloud_select=True)
    assert ("extract", "qwen3:latest") in used          # transcript stayed local
    assert ("select", "gpt-oss:120b-cloud") in used     # only the candidates travelled


def test_asking_for_the_cloud_without_configuring_it_is_an_error_not_a_fallback(tmp_path):
    """A silent downgrade would mean two different models wrote the same board with
    nothing saying which."""
    from dataclasses import dataclass

    from plaudvault import llm

    @dataclass
    class Cfg:
        cloud_model: str = ""
        llm_num_ctx: int = 8192
        cloud_num_ctx: int = 131072

    with pytest.raises(llm.LLMError) as exc:
        llm.with_cloud(Cfg())
    assert "cloud_tier_scope" in str(exc.value)


def test_the_context_window_travels_with_the_provider():
    """8192 is what a local 8B holds on a 24 GB machine. Reaching for a hosted model
    is precisely the case where that number is the constraint, so a cloud config that
    kept it would be a 1M-token model addressed through an 8k window."""
    from dataclasses import dataclass

    from plaudvault import llm

    @dataclass
    class Cfg:
        cloud_model: str = "glm-5.3:cloud"
        llm_num_ctx: int = 8192
        cloud_num_ctx: int = 131072
        llm_provider: str = "ollama"
        ollama_model: str = "qwen3:latest"
        openai_model: str = ""

    assert llm.with_cloud(Cfg()).llm_num_ctx == 131072
