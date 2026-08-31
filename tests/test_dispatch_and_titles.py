"""The two rules that keep agent dispatch safe, and the one that keeps titles honest.

Dispatch hands work to something that can act in the world, so the guards are the
product: only an accepted action can be handed over, a claim is atomic so two agents
cannot both believe they won it, and an agent's report is a report — not a completion.

Titles are the opposite kind of risk: nothing dangerous, just useless. A model that
cannot find a subject reaches for "Business Discussion", and thirty of those are no
better than thirty timestamps, so the cleaner refuses them.
"""

from __future__ import annotations

import time

import pytest

from plaudvault import dispatch
from plaudvault.titles import _clean


@pytest.fixture
def archive(tmp_path, monkeypatch):
    monkeypatch.setenv("PLAUDVAULT_ARCHIVE_ROOT", str(tmp_path / "archive"))
    monkeypatch.setenv("PLAUDVAULT_VAULT_ROOT", "")
    monkeypatch.setenv("PLAUDVAULT_CONFIG", str(tmp_path / "absent.toml"))

    from plaudvault.config import load
    from plaudvault.store import Store

    cfg = load()
    cfg.ensure_dirs()
    store = Store(cfg.db_path)
    yield cfg, store
    store.close()


def _action(store, status="accepted", text="Email the clinic contact"):
    return store.add_action(text=text, status=status, quote="I'll email them tomorrow")


# --------------------------------------------------------------------- dispatch


def test_a_proposed_action_cannot_be_dispatched(archive):
    """The extractor over-proposes by design, so acceptance is what a human read."""
    cfg, store = archive
    aid = _action(store, status="proposed")

    with pytest.raises(dispatch.NotDispatchable, match="accept it before"):
        dispatch.assign(cfg, store, aid, "openclaw")


@pytest.mark.parametrize("status", ["done", "dropped"])
def test_a_closed_action_cannot_be_dispatched(archive, status):
    cfg, store = archive
    aid = _action(store, status=status)

    with pytest.raises(dispatch.NotDispatchable):
        dispatch.assign(cfg, store, aid, "openclaw")


def test_an_unknown_agent_is_refused(archive):
    """An allow-list means a typo cannot park a job in a queue nobody polls."""
    cfg, store = archive
    aid = _action(store)

    with pytest.raises(ValueError, match="unknown agent"):
        dispatch.assign(cfg, store, aid, "rouge")


def test_one_live_job_per_action(archive):
    """Two agents doing the same thing, neither knowing, is worse than neither doing it."""
    cfg, store = archive
    aid = _action(store)
    dispatch.assign(cfg, store, aid, "openclaw")

    with pytest.raises(dispatch.NotDispatchable, match="already queued"):
        dispatch.assign(cfg, store, aid, "claude")


def test_a_cancelled_job_frees_the_action(archive):
    cfg, store = archive
    aid = _action(store)
    d = dispatch.assign(cfg, store, aid, "openclaw")

    dispatch.cancel(store, d["id"])

    again = dispatch.assign(cfg, store, aid, "claude")
    assert again["status"] == "queued"


def test_only_one_agent_can_claim_a_job(archive):
    """The status guard is in the UPDATE, not a read-then-write."""
    cfg, store = archive
    aid = _action(store)
    d = dispatch.assign(cfg, store, aid, "openclaw")

    first = dispatch.claim(cfg, store, d["id"], "openclaw@vm-1")
    assert first["status"] == "claimed"
    assert first["claimed_by"] == "openclaw@vm-1"

    with pytest.raises(dispatch.NotDispatchable, match="not queued"):
        dispatch.claim(cfg, store, d["id"], "openclaw@vm-2")


def test_claiming_moves_the_action_to_in_progress(archive):
    """So the board does not show a commitment untouched while an agent is on it."""
    cfg, store = archive
    aid = _action(store)
    d = dispatch.assign(cfg, store, aid, "openclaw")

    dispatch.claim(cfg, store, d["id"], "openclaw")

    assert store.get_action(aid)["status"] == "in_progress"


def test_a_report_does_not_close_the_action(archive):
    """An agent that believes it booked a meeting and did not must not tick the box."""
    cfg, store = archive
    aid = _action(store)
    d = dispatch.assign(cfg, store, aid, "openclaw")
    dispatch.claim(cfg, store, d["id"], "openclaw")

    out = dispatch.report(cfg, store, d["id"], ok=True, result="Sent; awaiting reply.")

    assert out["status"] == "done"
    assert store.get_action(aid)["status"] == "in_progress"  # still yours to close


def test_an_unclaimed_job_cannot_be_reported_on(archive):
    cfg, store = archive
    aid = _action(store)
    d = dispatch.assign(cfg, store, aid, "openclaw")

    with pytest.raises(dispatch.NotDispatchable, match="not claimed"):
        dispatch.report(cfg, store, d["id"], ok=True, result="done!")


def test_the_brief_carries_the_evidence(archive):
    """An agent told to act with no source cannot tell a real commitment from a garbled one."""
    cfg, store = archive
    aid = _action(store)
    d = dispatch.assign(cfg, store, aid, "openclaw", instructions="use my work address")

    brief = dispatch.brief(store, store.get_dispatch(d["id"]))

    assert brief["task"] == "Email the clinic contact"
    assert brief["instructions"] == "use my work address"
    assert brief["evidence"]["quote"] == "I'll email them tomorrow"


def test_summary_counts_unreviewed_reports(archive):
    cfg, store = archive
    aid = _action(store)
    d = dispatch.assign(cfg, store, aid, "openclaw")
    dispatch.claim(cfg, store, d["id"], "openclaw")
    dispatch.report(cfg, store, d["id"], ok=False, error="no calendar access")

    assert dispatch.summary(cfg, store)["unreviewed"] == 1

    dispatch.review(store, d["id"])
    assert dispatch.summary(cfg, store)["unreviewed"] == 0


# ----------------------------------------------------------------------- titles


@pytest.mark.parametrize(
    "raw,expected",
    [
        ('"Clinic pilot scope with the co-founder"', "Clinic pilot scope with the co-founder"),
        ("Title: Debugging the Postiz X integration", "Debugging the Postiz X integration"),
        ("**Corolla transmission quote**", "Corolla transmission quote"),
        ("Corolla transmission quote from Midas.", "Corolla transmission quote from Midas"),
    ],
)
def test_the_model_decorations_are_stripped(raw, expected):
    assert _clean(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "UNKNOWN",                       # the model said it could not name it
        "Discussion",                    # tells you nothing the archive did not know
        "General Conversation",
        "Meeting Recording 2026-07-14.",
        "A very long rambling sentence that the model produced instead of a title "
        "because it ignored the instruction and simply kept going",
        "",
    ],
)
def test_useless_titles_are_refused(raw):
    """An unnamed recording is honest. Thirty rows of "Business Discussion" are not."""
    assert _clean(raw) == ""


def test_a_human_title_is_not_overwritten_by_a_rerun(tmp_path, monkeypatch):
    """`--force` re-titles the machine's own work, never yours — as triage is never re-decided."""
    monkeypatch.setenv("PLAUDVAULT_ARCHIVE_ROOT", str(tmp_path / "archive"))
    monkeypatch.setenv("PLAUDVAULT_VAULT_ROOT", "")
    monkeypatch.setenv("PLAUDVAULT_CONFIG", str(tmp_path / "absent.toml"))

    from plaudvault.config import load
    from plaudvault.store import Store

    cfg = load()
    cfg.ensure_dirs()
    store = Store(cfg.db_path)

    class R:
        id, filename, start_time_ms = "r1", "r1.mp3", int(time.time()) * 1000
        duration_s, serial_number, file_md5, filesize, raw = 600.0, "sn", "", 1, {}

    store.upsert_remote(R())
    txt = cfg.transcript_dir / "r1.txt"
    txt.write_text("# x\n")
    store.update("r1", transcript_path=str(txt))

    store.set_title("r1", "My own name for this", source="human")
    candidates = [
        r["id"] for r in store.visible()
        if r["transcript_path"] and r["title_source"] != "human"
    ]
    assert candidates == []

    # And a titled recording is not in the queue for a first pass either.
    assert [r["id"] for r in store.needing_title()] == []
    store.close()


def test_a_recording_nothing_can_name_is_not_retried_forever(tmp_path, monkeypatch):
    """The titler looking and declining is a settled state, not a queue item.

    Without this the pipeline pays for a language-model call on every run that can
    never succeed, and the freshness pill sits amber over work nobody can do — the
    cry-wolf failure D11 exists to prevent. `sentiment_at` solves the identical
    problem; `titled_at` follows it.
    """
    monkeypatch.setenv("PLAUDVAULT_ARCHIVE_ROOT", str(tmp_path / "archive"))
    monkeypatch.setenv("PLAUDVAULT_VAULT_ROOT", "")
    monkeypatch.setenv("PLAUDVAULT_CONFIG", str(tmp_path / "absent.toml"))

    from plaudvault.config import load
    from plaudvault.store import Store

    cfg = load()
    cfg.ensure_dirs()
    store = Store(cfg.db_path)

    class R:
        id, filename, start_time_ms = "r1", "r1.mp3", int(time.time()) * 1000
        duration_s, serial_number, file_md5, filesize, raw = 600.0, "sn", "", 1, {}

    store.upsert_remote(R())
    txt = cfg.transcript_dir / "r1.txt"
    txt.write_text("# x\n")
    store.update("r1", transcript_path=str(txt))

    assert [r["id"] for r in store.needing_title()] == ["r1"]

    store.mark_title_attempted("r1")

    assert store.needing_title() == []
    assert store.get("r1")["title"] is None      # still honestly unnamed
    assert store.get("r1")["titled_at"] is not None
    store.close()
