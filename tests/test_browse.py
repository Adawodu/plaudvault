"""The link tree must be a view, never an authority.

Everything worth testing here is a way it could destroy or leak something. It runs
over the archive, it deletes files it considers stale, and it names things after a
tier decision — so the properties that matter are that it only ever removes its own
symlinks, that an excluded recording cannot appear in it, and that two recordings
that want the same name both survive.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from plaudvault import browse


class _Row(dict):
    def __getitem__(self, k):
        return dict.get(self, k)


class _Store:
    def __init__(self, rows):
        self._rows = rows

    def visible(self):
        return self._rows


class _Cfg:
    def __init__(self, root):
        self.archive_root = root


def _rec(tmp, rid, title, started=1_788_000_000, dur=600.0, kinds=("audio",)):
    row = _Row(id=rid, title=title, started_at=started, duration_s=dur,
               audio_path=None, transcript_path=None, summary_path=None)
    if "audio" in kinds:
        p = tmp / f"{rid}.mp3"; p.write_bytes(b"x"); row["audio_path"] = str(p)
    if "transcript" in kinds:
        p = tmp / f"{rid}.txt"; p.write_text("hello"); row["transcript_path"] = str(p)
    return row


def test_a_link_points_at_the_real_file(tmp_path):
    src = tmp_path / "src"; src.mkdir()
    rec = _rec(src, "abc123def456", "Board meeting")
    out = browse.rebuild(_Cfg(tmp_path), _Store([rec]))
    assert out["created"] == 1
    links = list((tmp_path / "by-name" / "audio").iterdir())
    assert len(links) == 1
    assert links[0].is_symlink()
    assert links[0].resolve() == Path(rec["audio_path"]).resolve()
    assert "Board meeting" in links[0].name


def test_rebuilding_changes_nothing(tmp_path):
    src = tmp_path / "src"; src.mkdir()
    store = _Store([_rec(src, "abc123def456", "Board meeting")])
    browse.rebuild(_Cfg(tmp_path), store)
    again = browse.rebuild(_Cfg(tmp_path), store)
    assert (again["created"], again["replaced"], again["removed"]) == (0, 0, 0)


def test_a_retitled_recording_repoints_rather_than_accumulating(tmp_path):
    src = tmp_path / "src"; src.mkdir()
    rec = _rec(src, "abc123def456", "Board meeting")
    browse.rebuild(_Cfg(tmp_path), _Store([rec]))
    rec["title"] = "Compliance audit"
    out = browse.rebuild(_Cfg(tmp_path), _Store([rec]))
    names = [p.name for p in (tmp_path / "by-name" / "audio").iterdir()]
    assert len(names) == 1 and "Compliance audit" in names[0]
    assert out["removed"] == 1


def test_a_recording_that_leaves_visible_loses_its_link(tmp_path):
    """The only enforcement that matters: untier something and it must disappear
    from a directory a person browses, on the next run and without being asked."""
    src = tmp_path / "src"; src.mkdir()
    rec = _rec(src, "abc123def456", "Private conversation")
    browse.rebuild(_Cfg(tmp_path), _Store([rec]))
    out = browse.rebuild(_Cfg(tmp_path), _Store([]))
    assert out["removed"] == 1
    assert not list((tmp_path / "by-name" / "audio").iterdir())


def test_a_real_file_in_the_tree_is_never_deleted(tmp_path):
    """It reconciles by deleting, and it runs unattended over an external drive.
    If a real file ever lands in here, losing it must not be a possible outcome."""
    browse.rebuild(_Cfg(tmp_path), _Store([]))
    stray = tmp_path / "by-name" / "audio" / "notes-i-put-here.txt"
    stray.write_text("mine")
    browse.rebuild(_Cfg(tmp_path), _Store([]))
    assert stray.exists() and stray.read_text() == "mine"


def test_two_recordings_wanting_one_name_both_survive(tmp_path):
    src = tmp_path / "src"; src.mkdir()
    a = _rec(src, "aaaaaaaaaaaa", "Standup")
    b = _rec(src, "bbbbbbbbbbbb", "Standup")
    browse.rebuild(_Cfg(tmp_path), _Store([a, b]))
    names = sorted(p.name for p in (tmp_path / "by-name" / "audio").iterdir())
    assert len(names) == 2 and names[0] != names[1]


def test_a_title_cannot_escape_its_directory(tmp_path):
    src = tmp_path / "src"; src.mkdir()
    rec = _rec(src, "abc123def456", "../../etc/passwd")
    browse.rebuild(_Cfg(tmp_path), _Store([rec]))
    links = list((tmp_path / "by-name" / "audio").iterdir())
    assert len(links) == 1
    assert links[0].parent == tmp_path / "by-name" / "audio"


def test_an_untitled_recording_still_gets_a_usable_name(tmp_path):
    src = tmp_path / "src"; src.mkdir()
    browse.rebuild(_Cfg(tmp_path), _Store([_rec(src, "abc123def456", None)]))
    name = next(iter((tmp_path / "by-name" / "audio").iterdir())).name
    assert name.startswith("20") and "m" in name


def test_a_missing_source_file_produces_no_link(tmp_path):
    src = tmp_path / "src"; src.mkdir()
    rec = _rec(src, "abc123def456", "Gone")
    Path(rec["audio_path"]).unlink()
    out = browse.rebuild(_Cfg(tmp_path), _Store([rec]))
    assert out["links"] == 0
