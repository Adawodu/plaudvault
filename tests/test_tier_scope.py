"""The one enforcement point, tested as one.

Tiering is the product's safety property: `mcp_tier_scope` decides what an MCP client
may read, and `exclude` is not readable at any scope. Both were enforced by a single
six-line function with no test under it, which is exactly the shape of thing that
survives a refactor while quietly failing open. A tier scope that fails open sends a
therapy session to whatever agent happened to connect.
"""

from __future__ import annotations

import time

from plaudvault import mcp_server as srv
from plaudvault import tiering
from plaudvault.store import Store

T0 = int(time.mktime((2026, 3, 10, 9, 0, 0, 0, 0, -1)))
ALL_TIERS = ["stack", "local", "untriaged", "exclude"]


class _Cfg:
    """Only what the tier path reads. A real Config would drag in the archive."""

    def __init__(self, db_path=None, tiers=("stack",), root=None):
        self.db_path = db_path
        self.mcp_tiers = set(tiers)
        self.archive_root = root
        self.summary_dir = root / "summaries" if root else None


def _seed(db, rows):
    st = Store(db)
    for rid, tier in rows:
        st.db.execute("INSERT INTO recordings (id, filename, started_at, duration_s) VALUES (?,?,?,?)",
                      (rid, f"{rid}.mp3", T0, 600.0))
        if tier:
            st.db.execute("INSERT INTO triage (recording_id, tier, decided_at) VALUES (?,?,?)",
                          (rid, tier, T0))
    st.db.commit()
    return st


# ------------------------------------------------------------------ _visible

def test_only_tiers_in_scope_are_visible():
    cfg = _Cfg(tiers=("stack",))
    assert srv._visible(cfg, None, "stack") is True
    assert srv._visible(cfg, None, "local") is False


def test_untriaged_is_its_own_scope_not_a_free_pass():
    """Nobody has judged an untriaged recording yet, which is a weaker claim than
    'local'. Treating NULL as permitted is how an unreviewed recording leaks."""
    assert srv._visible(_Cfg(tiers=("stack", "local")), None, None) is False
    assert srv._visible(_Cfg(tiers=("untriaged",)), None, None) is True


def test_exclude_is_unreachable_at_every_scope():
    """Not expressible in the scope, not readable when it is named anyway."""
    for scope in (("exclude",), ("stack", "local", "untriaged", "exclude"), ()):
        assert srv._visible(_Cfg(tiers=scope), None, "exclude") is False


def test_an_unknown_tier_is_refused_rather_than_assumed_safe():
    assert srv._visible(_Cfg(tiers=("stack", "local", "untriaged")), None, "quarantine") is False


def test_the_launch_override_narrows_and_does_not_widen(monkeypatch):
    """`plaudctl mcp --tiers stack` must be able to tighten the configured view."""
    cfg = _Cfg(tiers=("stack", "local"))
    monkeypatch.setattr(srv, "_TIER_OVERRIDE", {"stack"})
    assert srv._visible(cfg, None, "local") is False
    assert srv._visible(cfg, None, "stack") is True


# ------------------------------------------------- the tools that gate on it

def test_get_recording_refuses_out_of_scope_before_reading_anything(tmp_path, monkeypatch):
    db = tmp_path / "m.sqlite"
    _seed(db, [("r1", "local"), ("r2", "exclude"), ("r3", "stack")])
    cfg = _Cfg(db_path=db, tiers=("stack",), root=tmp_path)
    monkeypatch.setattr(srv, "_cfg", lambda: cfg)
    monkeypatch.setattr(srv, "_TIER_OVERRIDE", None)

    for rid in ("r1", "r2"):
        assert "outside this client's tier scope" in srv.get_recording(rid)
    assert "outside this client's tier scope" not in srv.get_recording("r3")


def test_get_transcript_gates_on_the_same_rule(tmp_path, monkeypatch):
    """Two read paths, one decision — a window into a transcript is still the
    transcript."""
    db = tmp_path / "m.sqlite"
    _seed(db, [("r1", "exclude"), ("r2", None)])
    cfg = _Cfg(db_path=db, tiers=("stack", "local"), root=tmp_path)
    monkeypatch.setattr(srv, "_cfg", lambda: cfg)
    monkeypatch.setattr(srv, "_TIER_OVERRIDE", None)

    assert "outside this client's tier scope" in srv.get_transcript("r1")
    assert "outside this client's tier scope" in srv.get_transcript("r2")  # untriaged


def test_a_missing_recording_does_not_leak_that_it_exists(tmp_path, monkeypatch):
    db = tmp_path / "m.sqlite"
    _seed(db, [])
    cfg = _Cfg(db_path=db, tiers=("stack",), root=tmp_path)
    monkeypatch.setattr(srv, "_cfg", lambda: cfg)
    assert "no such recording" in srv.get_recording("nope")


# ------------------------------------------------------- the physical mirror

def test_untiering_removes_the_copy_from_the_stack_directory(tmp_path):
    """A tier stored only in a database is a promise. PLAUD/stack/ is the fact, and
    the fact has to retract when the decision does."""
    db = tmp_path / "m.sqlite"
    st = _seed(db, [("r1", "stack"), ("r2", "local")])
    src = tmp_path / "r1.txt"
    src.write_text("[00:00:00] approved for the stack\n")
    st.db.execute("UPDATE recordings SET transcript_path = ? WHERE id = 'r1'", (str(src),))
    st.db.commit()

    cfg = _Cfg(db_path=db, root=tmp_path)
    assert tiering.sync(cfg, st)["added"] == 1
    assert (tiering.stack_dir(cfg) / "r1.txt").exists()

    st.db.execute("UPDATE triage SET tier = 'local' WHERE recording_id = 'r1'")
    st.db.commit()
    assert tiering.sync(cfg, st)["removed"] == 1
    assert not (tiering.stack_dir(cfg) / "r1.txt").exists()


def test_the_stack_directory_holds_copies_not_links(tmp_path):
    """An indexer that follows a symlink would silently read the whole corpus."""
    db = tmp_path / "m.sqlite"
    st = _seed(db, [("r1", "stack")])
    src = tmp_path / "r1.txt"
    src.write_text("body\n")
    st.db.execute("UPDATE recordings SET transcript_path = ? WHERE id = 'r1'", (str(src),))
    st.db.commit()
    cfg = _Cfg(db_path=db, root=tmp_path)
    tiering.sync(cfg, st)
    assert not (tiering.stack_dir(cfg) / "r1.txt").is_symlink()


def test_an_agent_cannot_write_onto_a_board_it_cannot_read(tmp_path, monkeypatch):
    """The tier scope bounds writing as well as reading. "Proposals are harmless" is
    not the argument — the board is the owner's, and an entry referencing a
    conversation this client was never shown is a scope violation whichever direction
    the data moved."""
    db = tmp_path / "m.sqlite"
    st = _seed(db, [("r1", "local"), ("r2", "stack")])
    cfg = _Cfg(db_path=db, tiers=("stack",), root=tmp_path)
    monkeypatch.setattr(srv, "_cfg", lambda: cfg)
    monkeypatch.setattr(srv, "_TIER_OVERRIDE", None)

    assert "outside this client's tier scope" in srv.propose_action("do a thing", "r1")
    assert st.actions(recording_id="r1") == []

    assert "action_id" in srv.propose_action("do a thing", "r2")
    assert len(st.actions(recording_id="r2")) == 1


def test_a_proposal_can_name_the_conversation_it_came_from(tmp_path, monkeypatch):
    db = tmp_path / "m.sqlite"
    st = _seed(db, [("r1", "stack")])
    cfg = _Cfg(db_path=db, tiers=("stack",), root=tmp_path)
    monkeypatch.setattr(srv, "_cfg", lambda: cfg)
    monkeypatch.setattr(srv, "_TIER_OVERRIDE", None)
    srv.propose_action("do a thing", "r1", segment=2)
    assert st.actions(recording_id="r1", segment_idx=2)
