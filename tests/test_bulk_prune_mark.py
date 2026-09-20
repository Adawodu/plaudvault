"""Queueing recordings for removal from somebody else's servers.

Marking is not deleting, which is the only reason it is allowed to be a bulk action:
`prune` re-checks every precondition at prune time, refuses to run until a probe has
proved the endpoint on one recording, and needs an explicit `--yes`. Nothing here
reaches Plaud.

What these hold down is the part that is easy to get wrong in a bulk path — that a
decision about somebody else's storage does not quietly become a decision about what a
recording *is*, and that the count a person is shown is the count that will act.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from plaudvault import web
from plaudvault.store import Store

T0 = int(time.mktime((2026, 3, 10, 9, 0, 0, 0, 0, -1)))


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = tmp_path / "m.sqlite"
    st = Store(db)
    for rid, tier in (("r1", "stack"), ("r2", "local"), ("r3", None)):
        st.db.execute("INSERT INTO recordings (id, filename, started_at, duration_s,"
                      " transcript_path) VALUES (?,?,?,?,?)",
                      (rid, f"{rid}.mp3", T0, 600.0, f"/tmp/{rid}.txt"))
        if tier:
            st.db.execute("INSERT INTO triage (recording_id, tier, decided_at)"
                          " VALUES (?,?,?)", (rid, tier, T0))
    st.db.commit()
    st.db.close()

    class _Cfg:
        db_path = db
        archive_root = tmp_path

        def check_archive_available(self):
            pass

    monkeypatch.setattr(web, "_cfg", lambda: _Cfg())
    monkeypatch.setattr(web.prune, "_eligible", lambda cfg, row: (row["id"] == "r1", "x"))
    return TestClient(web.app), db


def _triage(db, rec_id):
    with Store(db) as st:
        return st.triage_of(rec_id)


def test_marking_many_at_once(client):
    c, db = client
    r = c.post("/api/recordings/bulk/mark-for-deletion",
               json={"ids": ["r1", "r2", "r3"], "marked": True})
    assert r.json()["updated"] == 3
    assert all(_triage(db, i)["marked_for_prune"] for i in ("r1", "r2", "r3"))


def test_marking_does_not_change_what_a_recording_is(client):
    """Deciding to stop paying a company to store something says nothing about whether
    it belongs in your corpus. The bulk triage endpoint clears this flag for the same
    reason, from the other direction."""
    c, db = client
    c.post("/api/recordings/bulk/mark-for-deletion", json={"ids": ["r1", "r2"], "marked": True})
    assert _triage(db, "r1")["tier"] == "stack"
    assert _triage(db, "r2")["tier"] == "local"


def test_an_untriaged_recording_can_still_be_marked(client):
    """It gets a tier because the row needs one, and `local` is the choice that keeps
    it out of the corpus — marking must not quietly promote anything."""
    c, db = client
    c.post("/api/recordings/bulk/mark-for-deletion", json={"ids": ["r3"], "marked": True})
    assert _triage(db, "r3")["tier"] == "local"
    assert _triage(db, "r3")["marked_for_prune"]


def test_unmarking_is_the_same_gesture_backwards(client):
    c, db = client
    c.post("/api/recordings/bulk/mark-for-deletion", json={"ids": ["r1"], "marked": True})
    c.post("/api/recordings/bulk/mark-for-deletion", json={"ids": ["r1"], "marked": False})
    assert not _triage(db, "r1")["marked_for_prune"]
    assert _triage(db, "r1")["tier"] == "stack"


def test_the_response_says_how_many_actually_qualify_today(client):
    """Marking sixty and pruning four with no explanation is how a person stops
    trusting the number."""
    c, _ = client
    r = c.post("/api/recordings/bulk/mark-for-deletion",
               json={"ids": ["r1", "r2", "r3"], "marked": True}).json()
    assert r["updated"] == 3
    assert r["eligible_now"] == 1


def test_an_unknown_id_is_reported_not_silently_skipped(client):
    c, _ = client
    r = c.post("/api/recordings/bulk/mark-for-deletion",
               json={"ids": ["r1", "ghost"], "marked": True}).json()
    assert r["updated"] == 1 and r["missing"] == ["ghost"]


def test_an_empty_selection_is_refused(client):
    c, _ = client
    assert c.post("/api/recordings/bulk/mark-for-deletion", json={"ids": []}).status_code == 400


def test_a_runaway_selection_is_refused(client):
    """The same ceiling bulk triage applies. A request to mark thousands is a mistake
    more often than an intention."""
    c, _ = client
    r = c.post("/api/recordings/bulk/mark-for-deletion",
               json={"ids": [f"r{i}" for i in range(501)], "marked": True})
    assert r.status_code == 400


def test_marking_reaches_plaud_in_no_way_at_all(client, monkeypatch):
    """The safety claim this whole endpoint rests on."""
    import httpx

    def explode(*a, **k):
        raise AssertionError("marking must never call out to Plaud")

    monkeypatch.setattr(httpx, "post", explode)
    monkeypatch.setattr(httpx, "get", explode)
    c, _ = client
    assert c.post("/api/recordings/bulk/mark-for-deletion",
                  json={"ids": ["r1"], "marked": True}).json()["ok"]
