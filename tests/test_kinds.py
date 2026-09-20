"""A prayer is not a standup, and the extractor has to be told which it is looking at.

The archive's own numbers are the spec: a median of 12 actions per conversation against
a wanted 2-3, a maximum of 69, and 567 of 982 dropped by hand. One of those 69-action
recordings was a prayer session. These tests hold down the two decisions that follow —
a fixed vocabulary with a budget attached, and a human's classification outranking the
model's.
"""

from __future__ import annotations

import time

from plaudvault import kinds
from plaudvault.store import Store

T0 = int(time.mktime((2026, 3, 10, 9, 0, 0, 0, 0, -1)))


def _store(db, rows=(("r1", None),)):
    st = Store(db)
    for rid, tier in rows:
        st.db.execute("INSERT INTO recordings (id, filename, started_at, duration_s,"
                      " transcript_path) VALUES (?,?,?,?,?)",
                      (rid, f"{rid}.mp3", T0, 600.0, f"/tmp/{rid}.txt"))
        if tier:
            st.db.execute("INSERT INTO triage (recording_id, tier, decided_at) VALUES (?,?,?)",
                          (rid, tier, T0))
    st.db.commit()
    return st


# ------------------------------------------------------------------ vocabulary

def test_every_kind_carries_a_budget_and_a_description():
    for kind, (bud, desc) in kinds.KINDS.items():
        assert isinstance(bud, int) and bud >= 0, kind
        assert desc.strip(), kind


def test_the_kinds_that_yield_nothing_are_not_extracted():
    """Not 'extracted and found nothing' — never asked. Asking a devotional fifteen
    times, once per chunk, is what produced 69 action items."""
    assert not kinds.extractable("media")
    assert kinds.extractable("working")
    assert kinds.extractable("product")


def test_a_teaching_conversation_keeps_room_for_one_commitment():
    """Measured, not assumed: a sermon on spiritual leadership produced "commit to
    breaking bread with someone at least once a month", which the owner is acting on.
    A budget of 0 here would have hidden it."""
    assert kinds.extractable("devotional")
    assert kinds.budget("devotional") == 1


def test_a_conversation_yields_two_or_three_actions_not_twelve():
    assert kinds.budget("working") == 3
    assert kinds.budget("personal") == 2
    assert max(b for b, _ in kinds.KINDS.values()) <= 3


def test_an_unclassified_recording_is_budgeted_not_blocked():
    """No kind yet must not mean no actions — that would silently stop extraction on
    everything recorded before this feature existed."""
    assert kinds.extractable(None)
    assert kinds.budget(None) == kinds.budget("other")


# --------------------------------------------------------------------- parsing

def test_a_clean_classification_is_read():
    got = kinds._parse('{"kind": "devotional", "confidence": 0.9, "why": "prayer"}')
    assert got == {"kind": "devotional", "confidence": 0.9, "why": "prayer"}


def test_a_fenced_reply_is_read():
    got = kinds._parse('```json\n{"kind": "working", "confidence": 0.7, "why": "standup"}\n```')
    assert got["kind"] == "working"


def test_an_invented_kind_lands_on_other_and_says_so():
    """Coercing it to a working session would hand a budget to something nobody
    classified. `other` carries the same budget but records what happened."""
    got = kinds._parse('{"kind": "therapy", "confidence": 0.9}')
    assert got["kind"] == "other"
    assert got["confidence"] == 0.0
    assert "therapy" in got["why"]


def test_an_unparseable_confidence_is_treated_as_low():
    got = kinds._parse('{"kind": "working", "confidence": "very"}')
    assert got["confidence"] == 0.3


def test_a_reply_with_no_json_is_no_classification():
    assert kinds._parse("I think this was a work meeting.") is None
    assert kinds._parse("") is None


def test_an_empty_conversation_is_not_classified(monkeypatch):
    """Classifying from nothing would invent a kind, and the kind decides whether
    extraction runs at all."""
    monkeypatch.setattr(kinds, "_generate", lambda *a, **k: '{"kind":"working"}')
    assert kinds.classify_text(object(), "   ") is None


def test_the_tier_reaches_the_provider_call(monkeypatch):
    seen = []
    monkeypatch.setattr(kinds, "_generate", lambda cfg, p, *, tier=None, **kw: (
        seen.append(tier) or '{"kind": "working", "confidence": 0.8}'))
    kinds.classify_text(object(), "a summary of the call", tier="local")
    assert seen == ["local"]


# ---------------------------------------------------------------- persistence

def test_a_kind_round_trips(tmp_path):
    st = _store(tmp_path / "m.sqlite")
    st.set_kind("r1", kind="devotional", confidence=0.9, why="prayer", source="model")
    row = st.kind_of("r1")
    assert row["kind"] == "devotional" and row["source"] == "model"


def test_a_model_run_never_overwrites_a_human(tmp_path):
    """The kind decides whether extraction runs at all, so a silent reclassification
    changes what reaches the board. A sitting spent correcting it must survive."""
    st = _store(tmp_path / "m.sqlite")
    st.set_kind("r1", kind="personal", source="human")
    st.set_kind("r1", kind="media", confidence=0.99, source="model")
    assert st.kind_of("r1")["kind"] == "personal"


def test_a_human_can_change_their_own_mind(tmp_path):
    st = _store(tmp_path / "m.sqlite")
    st.set_kind("r1", kind="personal", source="human")
    st.set_kind("r1", kind="working", source="human")
    assert st.kind_of("r1")["kind"] == "working"


def test_only_unclassified_conversations_are_queued(tmp_path):
    st = _store(tmp_path / "m.sqlite", [("r1", None), ("r2", None)])
    st.set_kind("r1", kind="working", source="model")
    assert [c["recording_id"] for c in st.needing_kind()] == ["r2"]


def test_force_requeues_everything_but_still_protects_a_human(tmp_path):
    st = _store(tmp_path / "m.sqlite", [("r1", None), ("r2", None)])
    st.set_kind("r1", kind="personal", source="human")
    st.set_kind("r2", kind="working", source="model")
    assert {c["recording_id"] for c in st.needing_kind(force=True)} == {"r1", "r2"}
    st.set_kind("r1", kind="media", source="model")
    assert st.kind_of("r1")["kind"] == "personal"


def test_an_excluded_recording_is_never_queued(tmp_path):
    """`exclude` means out of the pipeline, not just out of the console."""
    st = _store(tmp_path / "m.sqlite", [("r1", "exclude"), ("r2", "local")])
    assert [c["recording_id"] for c in st.needing_kind()] == ["r2"]


# ------------------------------------------------- what extraction does with it

class _Cfg:
    """Only what extraction reads off the config."""

    extract_suggestions = False
    llm_label = "test"

    def __init__(self, tmp_path=None):
        # Selection reads the summary for context; a missing one is a valid state
        # (it selects with no context) and must not crash the run.
        self.summary_dir = (tmp_path / "summaries") if tmp_path else None


def _transcript(tmp_path, rid, body="[00:00:00] I'll send the deck on Friday.\n"):
    p = tmp_path / f"{rid}.txt"
    p.write_text(body)
    return p


def test_a_kind_that_expects_nothing_is_never_sent_to_the_model(tmp_path, monkeypatch):
    """The saving is not just a filtered list — the call is never made. A played-back
    podcast was being asked 'what commitments are here?' once per chunk, fifteen
    times, for a conversation nobody in the room was part of."""
    from plaudvault import extract

    st = _store(tmp_path / "m.sqlite", [("r1", None)])
    st.db.execute("UPDATE recordings SET transcript_path = ? WHERE id = 'r1'",
                  (str(_transcript(tmp_path, "r1")),))
    st.db.commit()
    st.set_kind("r1", kind="media", source="model")

    calls = []
    monkeypatch.setattr(extract, "available", lambda cfg: (True, "ok"))
    monkeypatch.setattr(extract, "read_transcript", lambda cfg, rid: "[00:00:00] Amen.\n")
    # The shared call site: map_prompts resolves `_generate` through the summarize
    # module at call time, so this covers the concurrent path as well as the serial one.
    monkeypatch.setattr("plaudvault.summarize._generate",
                        lambda *a, **k: calls.append(1) or "[]")

    stats = extract.run(_Cfg(tmp_path), st)
    assert calls == []
    assert stats["not_expected"] == 1
    assert stats["proposed"] == 0
    # Marked as handled, so it is not retried on every run.
    assert st.get("r1")["extracted_at"]


def test_an_extractable_kind_still_runs(tmp_path, monkeypatch):
    from plaudvault import extract

    st = _store(tmp_path / "m.sqlite", [("r1", None)])
    st.db.execute("UPDATE recordings SET transcript_path = ? WHERE id = 'r1'",
                  (str(_transcript(tmp_path, "r1")),))
    st.db.commit()
    st.set_kind("r1", kind="working", source="model")

    monkeypatch.setattr(extract, "available", lambda cfg: (True, "ok"))
    monkeypatch.setattr(extract, "read_transcript",
                        lambda cfg, rid: "[00:00:00] I'll send the deck on Friday.\n")
    monkeypatch.setattr("plaudvault.summarize._generate", lambda *a, **k: (
        '[{"text":"Send the deck","kind":"commitment","owner":"Bayo",'
        '"quote":"I\'ll send the deck on Friday","at":"00:00:00"}]'))

    stats = extract.run(_Cfg(tmp_path), st)
    assert stats["not_expected"] == 0
    assert stats["proposed"] == 1


def test_an_unclassified_recording_is_extracted_and_counted(tmp_path, monkeypatch):
    """No kind yet must not silently stop extraction on everything recorded before
    this existed — but the run says how many were in that state."""
    from plaudvault import extract

    st = _store(tmp_path / "m.sqlite", [("r1", None)])
    st.db.execute("UPDATE recordings SET transcript_path = ? WHERE id = 'r1'",
                  (str(_transcript(tmp_path, "r1")),))
    st.db.commit()

    monkeypatch.setattr(extract, "available", lambda cfg: (True, "ok"))
    monkeypatch.setattr(extract, "read_transcript", lambda cfg, rid: "[00:00:00] hello\n")
    monkeypatch.setattr("plaudvault.summarize._generate", lambda *a, **k: "[]")

    stats = extract.run(_Cfg(tmp_path), st)
    assert stats["unclassified"] == 1
    assert stats["not_expected"] == 0
