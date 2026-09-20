"""The pipeline runs per conversation, not per file.

Segments existed before this and nothing consumed them: `kinds`, `extract` and `brief`
still asked one question of a file holding four conversations. These tests hold the
wiring down — one kind per conversation, one budget per conversation, one brief per
conversation, and a recording-level clock that cannot be stamped until every
conversation inside it has been handled.

The last one is not hypothetical. `extracted_at` is a column on the recording, and
stamping it after the first of four segments marked the whole file done and skipped the
other three on the next run.
"""

from __future__ import annotations

import time

from plaudvault import brief, extract, kinds
from plaudvault.store import Store

T0 = int(time.mktime((2026, 3, 10, 9, 0, 0, 0, 0, -1)))
MIN = 60_000

TRANSCRIPT = "\n".join(
    [f"[00:{m:02d}:00] first conversation, minute {m}" for m in range(0, 30)]
    + [f"[00:{m:02d}:00] second conversation, minute {m}" for m in range(30, 60)]
)


class _Cfg:
    extract_suggestions = False
    llm_label = "test"

    def __init__(self, root):
        self.summary_dir = root / "summaries"
        self.brief_dir = root / "briefs"
        self.brief_dir.mkdir(parents=True, exist_ok=True)
        self.diarization_dir = root
        self.archive_root = root

    def ensure_dirs(self):
        pass


def _store(db, *, segmented=True, duration_s=3600.0):
    st = Store(db)
    st.db.execute("INSERT INTO recordings (id, filename, started_at, duration_s, title,"
                  " transcript_path, downloaded_at) VALUES (?,?,?,?,?,?,?)",
                  ("r1", "r1.mp3", T0, duration_s, "One long file", "/tmp/r1.txt", T0))
    st.db.commit()
    if segmented:
        st.set_segments("r1", [{"start_ms": 0, "end_ms": 30 * MIN},
                               {"start_ms": 30 * MIN, "end_ms": 60 * MIN}])
    return st


# ------------------------------------------------------------ conversations

def test_a_segmented_recording_is_two_conversations(tmp_path):
    st = _store(tmp_path / "m.sqlite")
    convs = st.conversations()
    assert [c["segment_idx"] for c in convs] == [0, 1]
    assert all(c["segmented"] for c in convs)
    assert convs[1]["label"].endswith("[2]")


def test_an_unsegmented_recording_is_one_conversation_with_a_plain_label(tmp_path):
    """The shape never depends on whether a file was segmented, and a recording that
    holds one conversation must not grow a "[1]" nobody asked for."""
    st = _store(tmp_path / "m.sqlite", segmented=False)
    convs = st.conversations()
    assert len(convs) == 1 and convs[0]["segment_idx"] == 0
    assert convs[0]["segmented"] is False
    assert convs[0]["label"] == "One long file"


def test_an_excluded_recording_is_in_no_conversation(tmp_path):
    st = _store(tmp_path / "m.sqlite")
    st.db.execute("INSERT INTO triage (recording_id, tier, decided_at) VALUES (?,?,?)",
                  ("r1", "exclude", T0))
    st.db.commit()
    assert st.conversations() == []


# ------------------------------------------------------------------- kinds

def test_each_conversation_gets_its_own_kind(tmp_path):
    """The mistake this whole view exists to correct: one file holding a standup and a
    school run is not one kind of thing."""
    st = _store(tmp_path / "m.sqlite")
    st.set_kind("r1", segment_idx=0, kind="working", source="model")
    st.set_kind("r1", segment_idx=1, kind="personal", source="model")
    assert st.kind_of("r1", 0)["kind"] == "working"
    assert st.kind_of("r1", 1)["kind"] == "personal"


def test_confirming_one_conversation_does_not_confirm_the_others(tmp_path):
    st = _store(tmp_path / "m.sqlite")
    st.set_kind("r1", segment_idx=0, kind="product", source="human")
    st.set_kind("r1", segment_idx=1, kind="working", source="model")
    st.set_kind("r1", segment_idx=0, kind="media", source="model")     # refused
    st.set_kind("r1", segment_idx=1, kind="media", source="model")     # allowed
    assert st.kind_of("r1", 0)["kind"] == "product"
    assert st.kind_of("r1", 1)["kind"] == "media"


def test_every_conversation_is_queued_for_classification(tmp_path):
    st = _store(tmp_path / "m.sqlite")
    st.set_kind("r1", segment_idx=0, kind="working", source="model")
    pending = st.needing_kind()
    assert [c["segment_idx"] for c in pending] == [1]


def test_a_segment_is_classified_from_its_own_transcript(tmp_path, monkeypatch):
    """A segment has no summary of its own, and a summary of a file holding two
    conversations describes neither."""
    st = _store(tmp_path / "m.sqlite")
    cfg = _Cfg(tmp_path)
    monkeypatch.setattr(kinds, "read_transcript", lambda c, r: TRANSCRIPT)
    first = kinds.body_for(cfg, st, st.conversations()[0])
    second = kinds.body_for(cfg, st, st.conversations()[1])
    assert "first conversation" in first and "second conversation" not in first
    assert "second conversation" in second and "first conversation" not in second


def test_an_unsegmented_recording_is_still_classified_from_its_summary(tmp_path):
    """Denser evidence, and it already exists. Only a segment has to fall back to
    reading its own slice of the transcript."""
    st = _store(tmp_path / "m.sqlite", segmented=False)
    cfg = _Cfg(tmp_path)
    cfg.summary_dir.mkdir(parents=True, exist_ok=True)
    (cfg.summary_dir / "r1.md").write_text("a summary of the whole thing")
    assert kinds.body_for(cfg, st, st.conversations()[0]) == "a summary of the whole thing"


# ----------------------------------------------------------------- extract

def _wire(monkeypatch, per_conversation_actions=4, keep=1):
    monkeypatch.setattr(extract, "available", lambda cfg: (True, "ok"))
    monkeypatch.setattr(extract, "read_transcript", lambda cfg, rid: TRANSCRIPT)
    monkeypatch.setattr(extract, "extract_from_text", lambda cfg, text, **k: [
        {"text": f"Action {i} from {text[:30]}", "quote": f"I'll do {i}",
         "owner": "Bayo", "kind": "commitment", "at_ms": 0}
        for i in range(per_conversation_actions)])
    monkeypatch.setattr(
        "plaudvault.select._generate",
        lambda *a, **k: '{"keep": [' + ", ".join(str(n) for n in range(1, keep + 1)) + ']}')


def test_each_conversation_is_extracted_with_its_own_budget(tmp_path, monkeypatch):
    st = _store(tmp_path / "m.sqlite")
    st.set_kind("r1", segment_idx=0, kind="working", source="model")
    st.set_kind("r1", segment_idx=1, kind="personal", source="model")
    _wire(monkeypatch)

    stats = extract.run(_Cfg(tmp_path), st)
    assert stats["recordings"] == 2                       # two conversations, not one file
    assert len(st.actions(recording_id="r1", segment_idx=0, status="proposed")) == 1
    assert len(st.actions(recording_id="r1", segment_idx=1, status="proposed")) == 1
    # Nothing lost on either side of the boundary.
    assert len(st.actions(recording_id="r1")) == 8


def test_actions_carry_the_conversation_they_came_from(tmp_path, monkeypatch):
    st = _store(tmp_path / "m.sqlite")
    for i in (0, 1):
        st.set_kind("r1", segment_idx=i, kind="working", source="model")
    _wire(monkeypatch)
    extract.run(_Cfg(tmp_path), st)
    first = st.actions(recording_id="r1", segment_idx=0)
    second = st.actions(recording_id="r1", segment_idx=1)
    assert all("first conversation" in a["text"] for a in first)
    assert all("second conversation" in a["text"] for a in second)


def test_a_media_conversation_is_skipped_while_its_neighbour_is_not(tmp_path, monkeypatch):
    """The whole point: a podcast playing for half a recording must not silence the
    meeting in the other half."""
    st = _store(tmp_path / "m.sqlite")
    st.set_kind("r1", segment_idx=0, kind="media", source="model")
    st.set_kind("r1", segment_idx=1, kind="working", source="model")
    _wire(monkeypatch)

    stats = extract.run(_Cfg(tmp_path), st)
    assert stats["not_expected"] == 1
    assert st.actions(recording_id="r1", segment_idx=0) == []
    assert len(st.actions(recording_id="r1", segment_idx=1)) == 4


def test_the_recording_clock_waits_for_every_conversation(tmp_path, monkeypatch):
    """`extracted_at` is a column on the recording. Stamping it after the first of two
    conversations marked the whole file done and skipped the second forever."""
    st = _store(tmp_path / "m.sqlite")
    st.set_kind("r1", segment_idx=0, kind="working", source="model")
    st.set_kind("r1", segment_idx=1, kind="working", source="model")
    _wire(monkeypatch)

    # Only the first conversation is processed this pass.
    extract.run(_Cfg(tmp_path), st, limit=1)
    assert st.get("r1")["extracted_at"] is None, "clock stamped with work outstanding"

    extract.run(_Cfg(tmp_path), st)
    assert st.get("r1")["extracted_at"] is not None


def test_a_single_conversation_recording_still_stamps_its_clock(tmp_path, monkeypatch):
    st = _store(tmp_path / "m.sqlite", segmented=False)
    st.set_kind("r1", kind="working", source="model")
    _wire(monkeypatch)
    extract.run(_Cfg(tmp_path), st)
    assert st.get("r1")["extracted_at"] is not None


# ------------------------------------------------------------------ briefs

def test_each_conversation_gets_its_own_brief_file(tmp_path, monkeypatch):
    st = _store(tmp_path / "m.sqlite")
    cfg = _Cfg(tmp_path)
    for i in (0, 1):
        st.set_kind("r1", segment_idx=i, kind="product", source="human")
    monkeypatch.setattr(brief, "available", lambda cfg: (True, "ok"))
    monkeypatch.setattr(brief, "read_transcript", lambda cfg, rid: TRANSCRIPT)
    monkeypatch.setattr(brief, "_chunk", lambda t: [t])
    monkeypatch.setattr(brief, "_generate", lambda cfg, p, **k: f"## Intent\n{p[-40:]}")
    monkeypatch.setattr("plaudvault.summarize._generate", lambda cfg, p, **k: f"## Intent\n{p[-40:]}")

    stats = brief.run(cfg, st)
    assert stats["written"] == 2
    assert brief.brief_path(cfg, "r1", 0).exists()
    assert brief.brief_path(cfg, "r1", 1).exists()
    assert brief.brief_path(cfg, "r1", 0) != brief.brief_path(cfg, "r1", 1)


def test_a_brief_is_written_from_its_own_conversation(tmp_path, monkeypatch):
    st = _store(tmp_path / "m.sqlite")
    cfg = _Cfg(tmp_path)
    st.set_kind("r1", segment_idx=1, kind="product", source="human")
    seen = []
    monkeypatch.setattr(brief, "available", lambda cfg: (True, "ok"))
    monkeypatch.setattr(brief, "read_transcript", lambda cfg, rid: TRANSCRIPT)
    monkeypatch.setattr(brief, "_chunk", lambda t: [t])
    _gen = lambda cfg, p, **k: seen.append(p) or "## Intent\nx"  # noqa: E731
    monkeypatch.setattr(brief, "_generate", _gen)
    monkeypatch.setattr("plaudvault.summarize._generate", _gen)
    brief.run(cfg, st)
    assert seen and "second conversation" in seen[0]
    assert "first conversation" not in seen[0]


def test_only_the_confirmed_conversation_is_briefed(tmp_path, monkeypatch):
    st = _store(tmp_path / "m.sqlite")
    cfg = _Cfg(tmp_path)
    st.set_kind("r1", segment_idx=0, kind="product", source="model")   # not confirmed
    st.set_kind("r1", segment_idx=1, kind="product", source="human")
    monkeypatch.setattr(brief, "available", lambda cfg: (True, "ok"))
    monkeypatch.setattr(brief, "read_transcript", lambda cfg, rid: TRANSCRIPT)
    monkeypatch.setattr(brief, "_chunk", lambda t: [t])
    monkeypatch.setattr(brief, "_generate", lambda cfg, p, **k: "## Intent\nx")
    monkeypatch.setattr("plaudvault.summarize._generate", lambda cfg, p, **k: "## Intent\nx")

    stats = brief.run(cfg, st)
    assert stats["written"] == 1 and stats["awaiting_confirmation"] == 1
    assert not brief.brief_path(cfg, "r1", 0).exists()
    assert brief.brief_path(cfg, "r1", 1).exists()


# ---------------------------------------------------------------- migration

def test_pre_existing_rows_mean_the_whole_recording(tmp_path):
    """Every action and kind written before segments existed described the whole file,
    which is exactly what segment 0 of an unsegmented recording means. That is why the
    column default backfills the archive correctly with no data migration."""
    st = _store(tmp_path / "m.sqlite", segmented=False)
    aid = st.add_action(recording_id="r1", text="from before", kind="commitment")
    assert st.get_action(aid)["segment_idx"] == 0
    st.set_kind("r1", kind="working", source="model")
    assert st.kind_of("r1")["segment_idx"] == 0
    assert st.kind_of("r1", 0)["kind"] == "working"


# ------------------------------------- what re-bounding a recording must do

def test_re_segmenting_drops_a_kind_the_model_inferred(tmp_path):
    """The kind described the conversation as it was bounded then. Re-bounding makes
    that claim about a conversation that no longer exists — and leaving it would sit on
    segment 0 looking current while `needing_kind` never queued it again."""
    st = _store(tmp_path / "m.sqlite", segmented=False)
    st.set_kind("r1", kind="working", source="model")
    st.set_segments("r1", [{"start_ms": 0, "end_ms": 30 * MIN},
                           {"start_ms": 30 * MIN, "end_ms": 60 * MIN}])
    assert st.kind_of("r1", 0) is None
    assert len(st.needing_kind()) == 2


def test_re_segmenting_keeps_a_kind_a_person_set(tmp_path):
    """D30: a human's decision outranks a re-run. They can change it themselves."""
    st = _store(tmp_path / "m.sqlite", segmented=False)
    st.set_kind("r1", kind="product", source="human")
    st.set_segments("r1", [{"start_ms": 0, "end_ms": 30 * MIN},
                           {"start_ms": 30 * MIN, "end_ms": 60 * MIN}])
    assert st.kind_of("r1", 0)["kind"] == "product"


def test_existing_actions_move_to_the_conversation_they_were_said_in(tmp_path):
    """An action carries the moment it was spoken, so re-bounding a file does not lose
    their placement — it recovers it."""
    st = _store(tmp_path / "m.sqlite", segmented=False)
    early = st.add_action(recording_id="r1", text="said early", kind="commitment",
                          at_ms=5 * MIN)
    late = st.add_action(recording_id="r1", text="said late", kind="commitment",
                         at_ms=45 * MIN)
    st.set_segments("r1", [{"start_ms": 0, "end_ms": 30 * MIN},
                           {"start_ms": 30 * MIN, "end_ms": 60 * MIN}])
    assert st.get_action(early)["segment_idx"] == 0
    assert st.get_action(late)["segment_idx"] == 1


def test_re_attribution_never_changes_a_status(tmp_path):
    """An accepted action stays accepted. It just stops being filed under the wrong
    conversation."""
    st = _store(tmp_path / "m.sqlite", segmented=False)
    aid = st.add_action(recording_id="r1", text="x", kind="commitment",
                        at_ms=45 * MIN, status="accepted")
    st.set_segments("r1", [{"start_ms": 0, "end_ms": 30 * MIN},
                           {"start_ms": 30 * MIN, "end_ms": 60 * MIN}])
    row = st.get_action(aid)
    assert row["status"] == "accepted" and row["segment_idx"] == 1


def test_an_action_with_no_timestamp_is_left_where_it_is(tmp_path):
    """A guess about where it belongs is worse than an honest default."""
    st = _store(tmp_path / "m.sqlite", segmented=False)
    aid = st.add_action(recording_id="r1", text="typed by hand", kind="manual")
    st.set_segments("r1", [{"start_ms": 0, "end_ms": 30 * MIN},
                           {"start_ms": 30 * MIN, "end_ms": 60 * MIN}])
    assert st.get_action(aid)["segment_idx"] == 0


def test_clearing_a_segmentation_brings_the_actions_home(tmp_path):
    """Back to one conversation means back to one board."""
    st = _store(tmp_path / "m.sqlite", segmented=False)
    aid = st.add_action(recording_id="r1", text="x", kind="commitment", at_ms=45 * MIN)
    st.set_segments("r1", [{"start_ms": 0, "end_ms": 30 * MIN},
                           {"start_ms": 30 * MIN, "end_ms": 60 * MIN}])
    assert st.get_action(aid)["segment_idx"] == 1
    st.clear_segments("r1")
    assert len(st.actions(recording_id="r1", segment_idx=0)) == 1
