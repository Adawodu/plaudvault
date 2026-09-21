"""Pruning an archive that has drifted from the account behind it.

Deleting in Plaud's own app is a legitimate workflow (D16), so a long-standing archive
ends up holding recordings the cloud no longer has — here, 54 of 72 eligible ones.
Attempting those is 54 doomed API calls reported as 54 failures, which buries the
handful that mattered and makes a working prune look broken.

These also hold down the things that must stay true while it reconciles: a recording
already gone is a goal reached rather than an error, an unmarked one is never touched,
and a listing that cannot be fetched degrades to attempting each rather than to
assuming the cloud is empty.
"""

from __future__ import annotations

import json
import time

from plaudvault import prune
from plaudvault.store import Store

T0 = int(time.mktime((2026, 3, 10, 9, 0, 0, 0, 0, -1)))


class _Cfg:
    prune_min_age_days = 14
    notes_dir = None

    def __init__(self, root):
        self.archive_root = root


class _Rec:
    def __init__(self, rid):
        self.id = rid


class _Client:
    """A cloud holding only what it is told, and counting what is sent to it."""

    def __init__(self, ids, fail_listing=False):
        self._ids = ids
        self.trashed = []
        self._fail = fail_listing

    def recordings(self, **kw):
        if self._fail:
            raise RuntimeError("network down")
        return [_Rec(i) for i in self._ids]

    def trash(self, file_id):
        self.trashed.append(file_id)
        return {"status": 0}


def _store(db, ids, *, marked=True):
    st = Store(db)
    for rid in ids:
        st.db.execute("INSERT INTO recordings (id, filename, started_at, duration_s,"
                      " transcript_path, audio_path, audio_sha256, size_ok, md5_verified,"
                      " noted_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (rid, f"{rid}.mp3", T0, 600.0, f"/tmp/{rid}.txt",
                       f"/tmp/{rid}.mp3", "hash", 1, 1, T0))
        st.db.execute("INSERT INTO triage (recording_id, tier, marked_for_prune,"
                      " decided_at) VALUES (?,?,?,?)", (rid, "local", int(marked), T0))
    st.db.commit()
    return st


def _setup(tmp_path, monkeypatch, local, cloud, *, marked=True, fail_listing=False):
    cfg = _Cfg(tmp_path)
    (tmp_path / prune.RECEIPT_NAME).write_text(json.dumps({"file_id": "x"}))
    st = _store(tmp_path / "m.sqlite", local, marked=marked)
    monkeypatch.setattr(prune, "_eligible", lambda c, row: (True, "ok"))
    monkeypatch.setattr(Store, "prunable",
                        lambda self, days, require_note=False: self.all())
    return cfg, st, _Client(cloud, fail_listing=fail_listing)


def test_a_recording_already_gone_is_not_attempted(tmp_path, monkeypatch):
    cfg, st, client = _setup(tmp_path, monkeypatch, ["a", "b", "c"], ["b"])
    stats = prune.run(cfg, client, st, confirm=True, limit=None)
    assert client.trashed == ["b"]
    assert stats["already_gone"] == 2
    assert stats["failed"] == 0


def test_already_gone_counts_as_reached_not_as_a_failure(tmp_path, monkeypatch):
    """It is the outcome pruning exists for. Filing it as an error is how a working
    run looks broken."""
    cfg, st, client = _setup(tmp_path, monkeypatch, ["a"], [])
    stats = prune.run(cfg, client, st, confirm=True, limit=None)
    assert stats == {"pruned": 0, "skipped": 0, "failed": 0, "already_gone": 1}


def test_a_recording_already_gone_stops_being_offered(tmp_path, monkeypatch):
    """`pruned_at` is stamped, so it does not reappear on every future run."""
    cfg, st, client = _setup(tmp_path, monkeypatch, ["a"], [])
    prune.run(cfg, client, st, confirm=True, limit=None)
    assert st.get("a")["pruned_at"] is not None


def test_an_unmarked_recording_is_never_touched(tmp_path, monkeypatch):
    """Marking is the consent step, and reconciling against the cloud must not become
    a way around it."""
    cfg, st, client = _setup(tmp_path, monkeypatch, ["a", "b"], ["a", "b"], marked=False)
    stats = prune.run(cfg, client, st, confirm=True, limit=None)
    assert client.trashed == []
    assert stats["skipped"] == 2 and stats["already_gone"] == 0


def test_a_dry_run_still_sends_nothing(tmp_path, monkeypatch):
    cfg, st, client = _setup(tmp_path, monkeypatch, ["a", "b"], ["a", "b"])
    prune.run(cfg, client, st, confirm=False, limit=None)
    assert client.trashed == []


def test_an_unreachable_listing_attempts_each_rather_than_assuming_empty(tmp_path, monkeypatch):
    """The dangerous failure would be reading a network error as "the cloud is empty"
    and marking everything gone without sending anything."""
    cfg, st, client = _setup(tmp_path, monkeypatch, ["a", "b"], [], fail_listing=True)
    stats = prune.run(cfg, client, st, confirm=True, limit=None)
    assert sorted(client.trashed) == ["a", "b"]
    assert stats["already_gone"] == 0


def test_without_a_receipt_nothing_runs_at_all(tmp_path, monkeypatch):
    """The probe gate stands in front of all of this."""
    cfg, st, client = _setup(tmp_path, monkeypatch, ["a"], ["a"])
    (tmp_path / prune.RECEIPT_NAME).unlink()
    stats = prune.run(cfg, client, st, confirm=True, limit=None)
    assert client.trashed == [] and stats["pruned"] == 0
