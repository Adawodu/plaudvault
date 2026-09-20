"""A brief is a working document, and the working part is what these protect.

The owner's exception to a board of three is a conversation that specifies something to
be built. The tempting fix is a larger budget; the right one is a different artifact,
because sixty checkboxes is a specification shredded into sixty pieces that have each
lost the context that made them meaningful.

Two properties carry the weight. A brief you have corrected by hand must survive a
re-run, because correcting it and handing it on is the entire point. And the `## Open`
section must be asked for explicitly, because a model summarising a design conversation
reports the decisions and quietly drops the disagreements — and an agent acting on the
decisions while unaware of what is unresolved is the failure this document prevents.
"""

from __future__ import annotations

import time

from plaudvault import brief
from plaudvault.store import Store

T0 = int(time.mktime((2026, 3, 10, 9, 0, 0, 0, 0, -1)))


class _Cfg:
    llm_label = "test"

    def __init__(self, root):
        self.archive_root = root
        self.brief_dir = root / "briefs"
        self.brief_dir.mkdir(parents=True, exist_ok=True)

    def ensure_dirs(self):
        pass


def _store(db, rows=(("r1", "product"),), source="human"):
    st = Store(db)
    for rid, kind in rows:
        st.db.execute("INSERT INTO recordings (id, filename, started_at, duration_s,"
                      " transcript_path) VALUES (?,?,?,?,?)",
                      (rid, f"{rid}.mp3", T0, 900.0, f"/tmp/{rid}.txt"))
        st.db.execute("INSERT INTO conversation_kinds (recording_id, kind, source, decided_at)"
                      " VALUES (?,?,?,?)", (rid, kind, source, T0))
    st.db.commit()
    return st


# ---------------------------------------------------------------- the prompt

def test_open_questions_are_asked_for_explicitly():
    """Not implied by a heading — a model that is shown an empty section fills it with
    nothing and reports success."""
    assert "## Open" in brief.PROMPT
    assert "None stated" in brief.PROMPT
    assert "not resolved" in brief.PROMPT.lower()


def test_the_merge_refuses_to_lose_an_open_question():
    """Merging partial briefs is where an unresolved question quietly disappears."""
    assert "keep every open question" in brief.MERGE_PROMPT.lower()


def test_a_brief_asks_for_timestamps():
    assert "[00:12:30]" in brief.PROMPT


# --------------------------------------------------------- generated vs human

def test_a_generated_brief_is_marked_as_generated(tmp_path, monkeypatch):
    monkeypatch.setattr(brief, "_chunk", lambda t: [t])
    monkeypatch.setattr(brief, "_generate", lambda *a, **k: "## Intent\nBuild the thing.")
    monkeypatch.setattr("plaudvault.summarize._generate", lambda *a, **k: "## Intent\nBuild the thing.")
    md = brief.write_brief(_Cfg(tmp_path), "[00:00:00] we should build it",
                           title="T", when="2026-03-10 09:00")
    assert brief.GENERATED_MARK in md
    assert "Build the thing." in md


def test_a_hand_edited_brief_is_detected_by_the_absence_of_the_mark(tmp_path):
    cfg = _Cfg(tmp_path)
    path = brief.brief_path(cfg, "r1")
    path.write_text(f"# Brief\n\n{brief.GENERATED_MARK}\n\n## Intent\nx\n")
    assert brief.was_edited(path) is False
    path.write_text("# Brief\n\n## Intent\nI rewrote this myself\n")
    assert brief.was_edited(path) is True


def test_a_missing_brief_is_not_an_edited_one(tmp_path):
    assert brief.was_edited(brief.brief_path(_Cfg(tmp_path), "nope")) is False


def test_the_mark_travels_inside_the_file(tmp_path, monkeypatch):
    """Provenance stored in the database would stop travelling the moment the file is
    copied, mailed, or pasted into an agent's context — which is what briefs are for."""
    monkeypatch.setattr(brief, "_chunk", lambda t: [t])
    monkeypatch.setattr(brief, "_generate", lambda *a, **k: "## Intent\nx")
    monkeypatch.setattr("plaudvault.summarize._generate", lambda *a, **k: "## Intent\nx")
    md = brief.write_brief(_Cfg(tmp_path), "[00:00:00] hi", title="T", when="now")
    assert brief.GENERATED_MARK in md.splitlines(keepends=False)[4:]


def test_force_still_refuses_to_overwrite_a_human(tmp_path, monkeypatch):
    """The one thing a re-run must never do, with or without --force."""
    cfg = _Cfg(tmp_path)
    st = _store(tmp_path / "m.sqlite")
    path = brief.brief_path(cfg, "r1")
    mine = "# Brief\n\n## Intent\nI wrote this and it is right\n"
    path.write_text(mine)

    monkeypatch.setattr(brief, "available", lambda cfg: (True, "ok"))
    monkeypatch.setattr(brief, "read_transcript", lambda cfg, rid: "[00:00:00] hello")
    monkeypatch.setattr(brief, "_generate", lambda *a, **k: "## Intent\nrobot text")
    monkeypatch.setattr("plaudvault.summarize._generate", lambda *a, **k: "## Intent\nrobot text")

    stats = brief.run(cfg, st, force=True)
    assert stats["kept"] == 1 and stats["written"] == 0
    assert path.read_text() == mine


# ------------------------------------------------------------------ selection

def test_only_product_conversations_are_briefed(tmp_path, monkeypatch):
    cfg = _Cfg(tmp_path)
    st = _store(tmp_path / "m.sqlite",
                [("r1", "product"), ("r2", "working"), ("r3", "personal")])
    monkeypatch.setattr(brief, "available", lambda cfg: (True, "ok"))
    monkeypatch.setattr(brief, "read_transcript", lambda cfg, rid: "[00:00:00] hello")
    monkeypatch.setattr(brief, "_chunk", lambda t: [t])
    monkeypatch.setattr(brief, "_generate", lambda *a, **k: "## Intent\nx")
    monkeypatch.setattr("plaudvault.summarize._generate", lambda *a, **k: "## Intent\nx")

    stats = brief.run(cfg, st)
    assert stats["written"] == 1
    assert brief.brief_path(cfg, "r1").exists()
    assert not brief.brief_path(cfg, "r2").exists()


def test_an_excluded_recording_is_never_briefed(tmp_path, monkeypatch):
    cfg = _Cfg(tmp_path)
    st = _store(tmp_path / "m.sqlite", [("r1", "product")])
    st.db.execute("INSERT INTO triage (recording_id, tier, decided_at) VALUES (?,?,?)",
                  ("r1", "exclude", T0))
    st.db.commit()
    monkeypatch.setattr(brief, "available", lambda cfg: (True, "ok"))
    stats = brief.run(cfg, st)
    assert stats["written"] == 0


def test_the_tier_reaches_the_provider_call(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(brief, "_chunk", lambda t: [t])
    _gen = lambda cfg, p, *, tier=None, **k: seen.append(tier) or "## Intent\nx"  # noqa: E731
    monkeypatch.setattr(brief, "_generate", _gen)
    monkeypatch.setattr("plaudvault.summarize._generate", _gen)
    brief.write_brief(_Cfg(tmp_path), "[00:00:00] hi", title="T", when="now", tier="stack")
    assert seen == ["stack"]


def test_nothing_usable_returns_no_brief_rather_than_an_empty_one(tmp_path, monkeypatch):
    monkeypatch.setattr(brief, "_chunk", lambda t: [t])
    monkeypatch.setattr(brief, "_generate", lambda *a, **k: "   ")
    monkeypatch.setattr("plaudvault.summarize._generate", lambda *a, **k: "   ")
    assert brief.write_brief(_Cfg(tmp_path), "[00:00:00] hi", title="T", when="now") == ""


# ------------------------------------------------------------------ the veto

def test_a_personal_conversation_produces_no_brief(tmp_path, monkeypatch):
    """The classifier reads a summary; this step reads the transcript and knows
    better. A recording of a personal argument was classified `product` at 0.95
    because a summary of it genuinely is business analysis."""
    monkeypatch.setattr(brief, "_chunk", lambda t: [t])
    monkeypatch.setattr(brief, "_generate", lambda *a, **k: brief.NOT_A_SPEC)
    monkeypatch.setattr("plaudvault.summarize._generate", lambda *a, **k: brief.NOT_A_SPEC)
    assert brief.write_brief(_Cfg(tmp_path), "[00:00:00] an argument",
                             title="T", when="now") == ""


def test_a_veto_in_any_part_vetoes_the_whole(tmp_path, monkeypatch):
    """One stretch of a conversation reading as a specification does not make the
    conversation one. The asymmetry is deliberate: no brief costs a command, the wrong
    brief costs whatever the agent does with it."""
    replies = iter(["## Intent\nBuild it.", brief.NOT_A_SPEC, "## Intent\nMore."])
    monkeypatch.setattr(brief, "_chunk", lambda t: ["a", "b", "c"])
    monkeypatch.setattr(brief, "_generate", lambda *a, **k: next(replies))
    monkeypatch.setattr("plaudvault.summarize._generate", lambda *a, **k: next(replies))
    assert brief.write_brief(_Cfg(tmp_path), "x", title="T", when="now") == ""


def test_a_declined_brief_is_reported_as_declined_not_failed(tmp_path, monkeypatch):
    cfg = _Cfg(tmp_path)
    st = _store(tmp_path / "m.sqlite")
    monkeypatch.setattr(brief, "available", lambda cfg: (True, "ok"))
    monkeypatch.setattr(brief, "read_transcript", lambda cfg, rid: "[00:00:00] hi")
    monkeypatch.setattr(brief, "_chunk", lambda t: [t])
    monkeypatch.setattr(brief, "_generate", lambda *a, **k: brief.NOT_A_SPEC)
    monkeypatch.setattr("plaudvault.summarize._generate", lambda *a, **k: brief.NOT_A_SPEC)
    stats = brief.run(cfg, st)
    assert stats["declined"] == 1 and stats["failed"] == 0 and stats["written"] == 0
    assert not brief.brief_path(cfg, "r1").exists()


def test_the_veto_instruction_survives_prompt_edits():
    assert brief.NOT_A_SPEC in brief.PROMPT
    assert "mostly personal" in brief.PROMPT


# -------------------------------------------------------- the acceptance gate

def test_a_brief_needs_a_human_confirmed_kind(tmp_path, monkeypatch):
    """The control that the veto failed to be. A recording classified `product` at 0.95
    confidence was a personal argument with business talk in the middle; a brief is the
    one artifact designed to travel into an agent's context, so a person confirms the
    kind before one is written — the same gate that stands between a proposed action and
    a dispatched one."""
    cfg = _Cfg(tmp_path)
    st = _store(tmp_path / "m.sqlite", source="model")
    monkeypatch.setattr(brief, "available", lambda cfg: (True, "ok"))
    monkeypatch.setattr(brief, "_generate", lambda *a, **k: "## Intent\nx")
    monkeypatch.setattr("plaudvault.summarize._generate", lambda *a, **k: "## Intent\nx")

    stats = brief.run(cfg, st)
    assert stats["written"] == 0
    assert stats["awaiting_confirmation"] == 1
    assert not brief.brief_path(cfg, "r1").exists()


def test_confirming_the_kind_by_hand_unlocks_the_brief(tmp_path, monkeypatch):
    cfg = _Cfg(tmp_path)
    st = _store(tmp_path / "m.sqlite", source="model")
    st.set_kind("r1", kind="product", source="human")
    monkeypatch.setattr(brief, "available", lambda cfg: (True, "ok"))
    monkeypatch.setattr(brief, "read_transcript", lambda cfg, rid: "[00:00:00] build it")
    monkeypatch.setattr(brief, "_chunk", lambda t: [t])
    monkeypatch.setattr(brief, "_generate", lambda *a, **k: "## Intent\nx")
    monkeypatch.setattr("plaudvault.summarize._generate", lambda *a, **k: "## Intent\nx")

    stats = brief.run(cfg, st)
    assert stats["written"] == 1 and stats["awaiting_confirmation"] == 0


def test_force_does_not_bypass_the_acceptance_gate(tmp_path, monkeypatch):
    """--force rewrites generated briefs. It does not manufacture consent."""
    cfg = _Cfg(tmp_path)
    st = _store(tmp_path / "m.sqlite", source="model")
    monkeypatch.setattr(brief, "available", lambda cfg: (True, "ok"))
    monkeypatch.setattr(brief, "_generate", lambda *a, **k: "## Intent\nx")
    monkeypatch.setattr("plaudvault.summarize._generate", lambda *a, **k: "## Intent\nx")
    assert brief.run(cfg, st, force=True)["written"] == 0
