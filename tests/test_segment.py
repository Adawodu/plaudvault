"""Conversations inside one file — a view over the recording, never a split of it.

A pin left running all day produces one recording holding several unrelated
conversations, and title, summary, tone, kind and budget are all per-conversation and
all wrong for it. The three worst-yielding recordings in the reference archive are this
shape.

The invariants these protect are the ones that make the feature safe rather than
clever: the audio is never touched, an unsegmented recording is one segment covering
the whole file, every millisecond belongs to exactly one segment, and a boundary a
person confirmed is never moved by a re-run.
"""

from __future__ import annotations

import json
import time

from plaudvault import segment
from plaudvault.store import Store

T0 = int(time.mktime((2026, 3, 10, 9, 0, 0, 0, 0, -1)))
MIN = 60_000


class _Cfg:
    def __init__(self, root):
        self.diarization_dir = root


def _store(db, duration_s=3600.0):
    st = Store(db)
    st.db.execute("INSERT INTO recordings (id, filename, started_at, duration_s, title,"
                  " downloaded_at) VALUES (?,?,?,?,?,?)",
                  ("r1", "r1.mp3", T0, duration_s, "One long file", T0))
    st.db.commit()
    return st


def _turns(*spans, speaker="SPEAKER_00"):
    """Normalised turns, as load_turns() hands them over — seconds in, ms out."""
    return [{"start_ms": int(a * 1000), "end_ms": int(b * 1000), "speaker": speaker}
            for a, b in spans]


# ------------------------------------------------- an unsegmented recording

def test_a_recording_with_no_segmentation_is_one_segment_covering_it(tmp_path):
    """Not a special case to handle at every call site — it is what an unsegmented
    recording has always meant, made explicit. Every existing recording has a valid
    segment list the day this ships."""
    st = _store(tmp_path / "m.sqlite", duration_s=1800)
    segs = st.segments("r1")
    assert len(segs) == 1
    assert segs[0]["start_ms"] == 0 and segs[0]["end_ms"] == 1_800_000
    assert segs[0]["source"] == "implicit"
    assert st.is_segmented("r1") is False


def test_the_implicit_segment_is_not_written_to_the_table(tmp_path):
    """Storing 94 rows that say "the whole thing" would turn a derived convenience
    into state that can drift from the recording it describes."""
    st = _store(tmp_path / "m.sqlite")
    st.segments("r1")
    assert st.db.execute("SELECT COUNT(*) c FROM segments").fetchone()["c"] == 0


def test_an_unknown_recording_has_no_segments(tmp_path):
    assert _store(tmp_path / "m.sqlite").segments("nope") == []


# ------------------------------------------------------------ boundaries

def test_a_long_silence_opens_a_boundary():
    turns = _turns((0, 100), (400, 500))          # 300s gap
    got = segment.candidates(turns, gap_ms=180_000)
    assert len(got) == 1
    assert got[0]["at_ms"] == 400_000
    assert "silence" in got[0]["why"]


def test_a_short_pause_does_not():
    """People stop to think. Below the threshold a gap is a pause, not an ending."""
    assert segment.candidates(_turns((0, 100), (160, 300)), gap_ms=180_000) == []


def test_a_change_of_cast_alone_never_creates_a_boundary():
    """The regression this detector was born with. Scoring speaker-label turnover
    produced 90 conversations from one 4.5-hour recording and 19 from a single
    interview, because diarization labels are noisy and unnamed — it was measuring the
    diarizer, not the conversation."""
    turns = [{"start_ms": i * 1000, "end_ms": (i + 1) * 1000,
              "speaker": f"SPEAKER_{i % 6:02d}"} for i in range(60)]
    assert segment.candidates(turns, gap_ms=180_000) == []


def test_a_cast_change_still_strengthens_a_silence_that_stands_alone():
    quiet = _turns((0, 100), (400, 500))
    plain = segment.candidates(quiet, gap_ms=180_000)[0]
    mixed = [{"start_ms": 0, "end_ms": 100_000, "speaker": "A"},
             {"start_ms": 400_000, "end_ms": 500_000, "speaker": "B"}]
    with_cast = segment.candidates(mixed, gap_ms=180_000)[0]
    assert with_cast["confidence"] > plain["confidence"]
    assert "cast changed" in with_cast["why"]


def test_a_longer_silence_is_a_stronger_claim():
    short = segment.candidates(_turns((0, 100), (300, 400)), gap_ms=180_000)[0]
    long_ = segment.candidates(_turns((0, 100), (1500, 1600)), gap_ms=180_000)[0]
    assert long_["confidence"] > short["confidence"]


# ---------------------------------------------------------------- the spans

def test_every_millisecond_belongs_to_exactly_one_segment():
    """A gap between segments would be audio that is in the archive and in no
    conversation — which is how a commitment goes missing with nothing reporting it."""
    got = segment.spans(60 * MIN, [{"at_ms": 20 * MIN, "confidence": 0.8, "why": "x"},
                                   {"at_ms": 40 * MIN, "confidence": 0.8, "why": "y"}])
    assert [(s["start_ms"], s["end_ms"]) for s in got] == [
        (0, 20 * MIN), (20 * MIN, 40 * MIN), (40 * MIN, 60 * MIN)]


def test_a_cut_that_would_leave_a_fragment_is_dropped():
    """A phone call taken mid-meeting should not become a peer of the meeting.
    Dropping the cut leaves the conversations joined, which is today's behaviour and
    therefore the safe direction to fail in."""
    got = segment.spans(60 * MIN, [{"at_ms": 1 * MIN, "confidence": 1, "why": "x"}],
                        min_segment_ms=2 * MIN)
    assert got == []


def test_a_cut_too_close_to_the_end_is_dropped():
    got = segment.spans(60 * MIN, [{"at_ms": 59 * MIN, "confidence": 1, "why": "x"}],
                        min_segment_ms=2 * MIN)
    assert got == []


def test_no_cuts_means_no_segmentation_rather_than_one_row():
    """One segment spanning the file is what an unsegmented recording already is."""
    assert segment.spans(60 * MIN, []) == []


def test_a_recording_with_no_duration_produces_nothing():
    assert segment.spans(0, [{"at_ms": 100, "confidence": 1, "why": "x"}]) == []


# ------------------------------------------------- proposed versus confirmed

def test_a_proposal_can_be_replaced_by_a_later_run(tmp_path):
    st = _store(tmp_path / "m.sqlite")
    st.set_segments("r1", [{"start_ms": 0, "end_ms": 100}, {"start_ms": 100, "end_ms": 200}])
    assert len(st.segments("r1")) == 2
    st.set_segments("r1", [{"start_ms": 0, "end_ms": 200}])
    assert len(st.segments("r1")) == 1


def test_a_confirmed_segmentation_is_never_moved_by_a_re_run(tmp_path):
    """Every decision downstream — a tier, an accepted action, a brief — hangs off a
    boundary. Moving one silently orphans all of them with nothing in the data saying
    when it happened."""
    st = _store(tmp_path / "m.sqlite")
    st.set_segments("r1", [{"start_ms": 0, "end_ms": 100}, {"start_ms": 100, "end_ms": 200}])
    st.confirm_segments("r1")
    assert st.set_segments("r1", [{"start_ms": 0, "end_ms": 200}]) == 0
    segs = st.segments("r1")
    assert len(segs) == 2 and all(s["source"] == "human" for s in segs)


def test_a_person_can_still_re_segment_by_hand(tmp_path):
    st = _store(tmp_path / "m.sqlite")
    st.set_segments("r1", [{"start_ms": 0, "end_ms": 200}])
    st.confirm_segments("r1")
    assert st.set_segments("r1", [{"start_ms": 0, "end_ms": 100},
                                  {"start_ms": 100, "end_ms": 200}], source="human") == 2


def test_clearing_returns_the_recording_to_one_conversation(tmp_path):
    """Derived data only. The audio and the transcript are untouched by any of this."""
    st = _store(tmp_path / "m.sqlite", duration_s=600)
    st.set_segments("r1", [{"start_ms": 0, "end_ms": 300_000},
                           {"start_ms": 300_000, "end_ms": 600_000}])
    st.clear_segments("r1")
    assert st.is_segmented("r1") is False
    assert len(st.segments("r1")) == 1


# ----------------------------------------------------- reading the transcript

TRANSCRIPT = "\n".join([
    "[00:00:05] first conversation opens",
    "[00:10:00] still the first",
    "[00:30:00] second conversation opens",
    "[00:45:00] still the second",
])


def test_a_segment_reads_its_own_lines_from_the_master_transcript():
    """The transcript is not cut on disk either — this is a view of it, so a segment's
    text can never drift from what the recording says."""
    first = segment.transcript_for(TRANSCRIPT, 0, 20 * MIN)
    assert "first conversation opens" in first
    assert "second conversation opens" not in first


def test_the_second_segment_starts_where_the_first_ended():
    second = segment.transcript_for(TRANSCRIPT, 20 * MIN, 60 * MIN)
    assert "second conversation opens" in second
    assert "still the first" not in second


def test_untimestamped_lines_are_skipped_rather_than_guessed():
    text = "a header with no timestamp\n[00:00:05] real line"
    assert segment.transcript_for(text, 0, MIN) == "[00:00:05] real line"


# ------------------------------------------------------------------ proposal

def test_a_recording_that_was_never_diarized_says_so(tmp_path):
    """Boundaries are read from turns. Without them this has no evidence at all, and
    saying that is better than returning one confident segment."""
    st = _store(tmp_path / "m.sqlite")
    got = segment.propose(_Cfg(tmp_path), st, "r1")
    assert got["spans"] == []
    assert "not diarized" in got["reason"]


def test_a_proposal_reads_turns_from_disk(tmp_path):
    st = _store(tmp_path / "m.sqlite", duration_s=3600)
    (tmp_path / "r1.json").write_text(json.dumps(
        {"turns": [{"start": 0, "end": 600, "speaker": "A"},
                   {"start": 1200, "end": 3600, "speaker": "A"}]}))
    got = segment.propose(_Cfg(tmp_path), st, "r1")
    assert len(got["spans"]) == 2
    assert got["spans"][0]["end_ms"] == 1_200_000


# --------------------------------------------------------- dead air is not a
# conversation

def test_a_span_that_is_mostly_silence_is_joined_to_its_neighbour():
    """Two long silences in a row leave a sliver between them. On a real recording one
    such span ran seven minutes and held 368 characters of speech: duration says
    conversation, the audio says nobody was talking."""
    spans_ = [{"start_ms": 0, "end_ms": 30 * MIN, "why": "a"},
              {"start_ms": 30 * MIN, "end_ms": 37 * MIN, "why": "b"},
              {"start_ms": 37 * MIN, "end_ms": 70 * MIN, "why": "c"}]
    turns = _turns((0, 25 * 60), (37 * 60, 65 * 60))      # nothing in the middle span
    got = segment.join_quiet(spans_, turns)
    assert len(got) == 2
    assert got[0]["end_ms"] == 37 * MIN                   # the sliver folded backwards
    assert "quiet stretch" in got[0]["why"]


def test_joining_keeps_the_recording_covered():
    """A span belonging to no segment is archive nobody can reach through the view."""
    spans_ = [{"start_ms": 0, "end_ms": 30 * MIN, "why": "a"},
              {"start_ms": 30 * MIN, "end_ms": 40 * MIN, "why": "b"}]
    got = segment.join_quiet(spans_, _turns((0, 25 * 60)))
    assert got == [] or (got[0]["start_ms"] == 0 and got[-1]["end_ms"] == 40 * MIN)


def test_one_surviving_span_is_no_segmentation_at_all():
    """Which is what an unsegmented recording already means."""
    spans_ = [{"start_ms": 0, "end_ms": 30 * MIN, "why": "a"},
              {"start_ms": 30 * MIN, "end_ms": 40 * MIN, "why": "b"}]
    assert segment.join_quiet(spans_, _turns((0, 25 * 60))) == []


def test_speech_time_clips_a_turn_to_the_span():
    turns = _turns((0, 600))                    # ten minutes of talking from zero
    assert segment.speech_ms(turns, 0, 5 * MIN) == 5 * MIN
    assert segment.speech_ms(turns, 20 * MIN, 30 * MIN) == 0


def test_a_busy_span_is_kept():
    spans_ = [{"start_ms": 0, "end_ms": 30 * MIN, "why": "a"},
              {"start_ms": 30 * MIN, "end_ms": 60 * MIN, "why": "b"}]
    turns = _turns((0, 25 * 60), (31 * 60, 55 * 60))
    assert len(segment.join_quiet(spans_, turns)) == 2
