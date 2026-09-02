"""Which seconds of audio you are played to identify somebody.

A sample is the evidence behind a name, and a name here becomes a voiceprint that
attributes speech in every later recording. So a bad sample is not a cosmetic
problem: it is how you confidently name the wrong person once and have the archive
agree with you from then on. What is tested is that a clip is long enough to
recognise, comes from the part of a turn the model is most sure about, and never
runs past the turn into whoever spoke next.
"""

from __future__ import annotations

import json

from plaudvault import diarize


class _Cfg:
    def __init__(self, root):
        self.diarization_dir = root
        root.mkdir(parents=True, exist_ok=True)


def _write(tmp, turns):
    cfg = _Cfg(tmp / "diar")
    (cfg.diarization_dir / "rec.json").write_text(json.dumps({"turns": turns}))
    return cfg


def test_a_clip_comes_from_the_middle_of_its_turn(tmp_path):
    """Diarization boundaries are where the model is least certain, so the first and
    last seconds of a turn are the likeliest to be somebody else's voice."""
    cfg = _write(tmp_path, [{"start": 100.0, "end": 160.0, "speaker": "A"}])
    s = diarize.samples(cfg, "rec", "A")[0]
    assert s["start"] > 100.0 and s["end"] < 160.0
    assert abs(((s["start"] + s["end"]) / 2) - 130.0) < 0.01


def test_a_clip_never_runs_past_its_turn(tmp_path):
    cfg = _write(tmp_path, [{"start": 10.0, "end": 12.0, "speaker": "A"},
                            {"start": 12.0, "end": 40.0, "speaker": "B"}])
    for s in diarize.samples(cfg, "rec", "A"):
        assert s["start"] >= 10.0 and s["end"] <= 12.0


def test_fragments_too_short_to_identify_anyone_are_skipped(tmp_path):
    cfg = _write(tmp_path, [{"start": 0.0, "end": 0.4, "speaker": "A"},
                            {"start": 5.0, "end": 5.3, "speaker": "A"}])
    assert diarize.samples(cfg, "rec", "A") == []


def test_the_longest_turns_win_but_play_in_time_order(tmp_path):
    cfg = _write(tmp_path, [
        {"start": 500.0, "end": 560.0, "speaker": "A"},   # longest
        {"start": 10.0, "end": 12.0, "speaker": "A"},     # shortest, still eligible
        {"start": 200.0, "end": 230.0, "speaker": "A"},   # middle
        {"start": 0.0, "end": 999.0, "speaker": "B"},
    ])
    got = diarize.samples(cfg, "rec", "A", n=2)
    assert len(got) == 2
    assert [g["turn_seconds"] for g in got] == [30.0, 60.0]   # picked long, ordered by time
    assert got[0]["start"] < got[1]["start"]


def test_a_clip_is_capped_so_naming_a_queue_stays_quick(tmp_path):
    cfg = _write(tmp_path, [{"start": 0.0, "end": 3600.0, "speaker": "A"}])
    s = diarize.samples(cfg, "rec", "A")[0]
    assert s["end"] - s["start"] <= diarize.SAMPLE_MAX_S + 0.01


def test_no_diarization_means_no_samples_not_an_error(tmp_path):
    """Most recordings are not diarized. The console asks anyway, and must get an
    empty list rather than a stack trace."""
    cfg = _Cfg(tmp_path / "diar")
    assert diarize.samples(cfg, "missing", "A") == []


def test_an_unknown_label_yields_nothing(tmp_path):
    cfg = _write(tmp_path, [{"start": 0.0, "end": 60.0, "speaker": "A"}])
    assert diarize.samples(cfg, "rec", "SPEAKER_99") == []


# ------------------------------------------------------- naming does not rewrite people

def test_naming_a_voice_does_not_un_me_the_person(tmp_path):
    """Inline naming sends a name and nothing else. Reusing an existing person must not
    reset their flags — confirming your own voice on a second recording once would have
    stopped the archive believing you are you."""
    from plaudvault.store import Store

    with Store(tmp_path / "m.sqlite") as st:
        sid = st.add_speaker("Bayo", is_me=True)
        again = st.add_speaker("Bayo")            # the inline path: name only
        assert again == sid
        assert st.speaker(sid)["is_me"] == 1

        st.add_speaker("Bayo", is_me=False)       # explicit, and must be honoured
        assert st.speaker(sid)["is_me"] == 0
