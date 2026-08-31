"""Answer one question, honestly: is the vault current, or is work outstanding?

"Up to date" is not one fact but several, and they fail independently. Plaud's cloud
can hold a recording that never reached the disk. The disk can hold audio that was
never transcribed because Ollama was down that morning. A note can be recorded in the
manifest and no longer exist in the vault because it was deleted or the vault moved.
The `stack/` corpus can disagree with the triage decisions that are supposed to
govern it. Each of those looks like a healthy archive from every angle except the
one that catches it, so this module checks all of them and refuses to say "up to
date" until every one is clear.

The cloud check is separate and optional: it costs a network call and an authenticated
session, and a laptop offline in a cafe should still be able to report on its own
disk rather than erroring out.
"""

from __future__ import annotations

import time
from pathlib import Path

from . import diarize, tiering
from .config import Config
from .store import Store

DAY = 86400


def _pending(store: Store, cfg: Config) -> list[dict]:
    """Work items the pipeline still owes, in the order the pipeline does them."""
    # Dismissed recordings are deliberately unprocessed, so counting them as pending
    # would leave the freshness pill permanently amber for work nobody wants done —
    # exactly the cry-wolf failure the verdict is designed to avoid.
    q = lambda w: store.db.execute(  # noqa: E731
        f"SELECT COUNT(*) FROM recordings r WHERE ({w}) AND {store.NOT_EXCLUDED}"
    ).fetchone()[0]

    items = [
        {
            "stage": "download",
            "count": q("audio_sha256 IS NULL OR audio_kind IS NULL"),
            "label": "downloaded from Plaud",
            "fix": "plaudctl sync",
        },
        {
            "stage": "transcribe",
            "count": q("transcript_path IS NULL AND audio_kind IS NOT NULL"),
            "label": "transcribed",
            "fix": "plaudctl transcribe",
        },
        {
            # Only counted when diarization is actually configured. The models are
            # gated behind a HuggingFace licence, so on a machine that has not set
            # that up this would be permanently outstanding work nobody can do — the
            # amber-forever indicator D11 and D15 both exist to prevent.
            "stage": "diarize",
            "count": q(
                "diarized_at IS NULL AND transcript_path IS NOT NULL "
                f"AND duration_s >= {int(cfg.diarize_min_seconds)}"
            ) if diarize.status(cfg)[0] else 0,
            "label": "split by speaker",
            "fix": "plaudctl diarize",
        },
        {
            "stage": "summarize",
            "count": q(
                "summary_path IS NULL AND transcript_path IS NOT NULL "
                f"AND duration_s >= {int(cfg.summarize_min_seconds)}"
            ),
            "label": "summarized",
            "fix": "plaudctl summarize",
        },
        {
            "stage": "title",
            # Keyed on `titled_at`, like sentiment: a recording the titler looked at
            # and could not name is settled, not outstanding. Counting it would leave
            # the pill amber forever over work that can never complete.
            "count": q("titled_at IS NULL AND transcript_path IS NOT NULL"),
            "label": "given a title",
            "fix": "plaudctl title",
        },
        {
            "stage": "sentiment",
            "count": q("sentiment_at IS NULL AND transcript_path IS NOT NULL"),
            "label": "scored for tone",
            "fix": "plaudctl sentiment",
        },
        {
            "stage": "extract",
            "count": q("extracted_at IS NULL AND transcript_path IS NOT NULL"),
            "label": "scanned for commitments",
            "fix": "plaudctl extract",
        },
        {
            # Keyed on the current embedding model: changing it leaves the corpus half
            # in one vector space and half in another, which reads as a silently
            # incomplete search rather than an error.
            "stage": "index",
            "count": store.db.execute(
                f"SELECT COUNT(*) FROM recordings r WHERE r.transcript_path IS NOT NULL "
                f"AND {store.NOT_EXCLUDED} "
                "AND NOT EXISTS (SELECT 1 FROM chunks c WHERE c.recording_id = r.id "
                "AND c.model = ?)",
                (cfg.embed_model,),
            ).fetchone()[0],
            "label": "indexed for search",
            "fix": "plaudctl index",
        },
    ]
    if cfg.notes_dir is not None:
        items += [
            {
                "stage": "notes",
                "count": q("note_path IS NULL AND transcript_path IS NOT NULL"),
                "label": "written into the vault",
                "fix": "plaudctl notes",
            },
            {
                "stage": "notes-stale",
                "count": store.db.execute(
                    "SELECT COUNT(*) FROM recordings r JOIN sentiment s "
                    "ON s.recording_id = r.id WHERE r.note_path IS NOT NULL "
                    "AND s.scored_at > COALESCE(r.noted_at, 0)"
                ).fetchone()[0],
                "label": "rewritten since their tone was scored",
                "fix": "plaudctl notes",
            },
        ]
    return [i for i in items if i["count"]]


def _missing_notes(store: Store) -> list[dict]:
    """Notes the manifest believes exist but the vault no longer has.

    Deleting a note in Obsidian, or moving the vault, leaves the manifest claiming a
    file that isn't there — and `notes` will never rewrite it, because the column is
    set. This is the only check that catches that, so it stats every path.
    """
    gone = []
    for row in store.db.execute(
        "SELECT id, filename, note_path FROM recordings WHERE note_path IS NOT NULL"
    ):
        if not Path(row["note_path"]).exists():
            gone.append({"id": row["id"], "filename": row["filename"], "path": row["note_path"]})
    return gone


def _stack_drift(cfg: Config, store: Store) -> dict:
    """What `plaudctl tier` would change — computed without changing anything."""
    target = tiering.stack_dir(cfg)
    approved = {
        f"{row['id']}.txt": Path(row["transcript_path"])
        for row in store.by_tier("stack")
        if row["transcript_path"] and Path(row["transcript_path"]).exists()
    }
    present = {p.name: p for p in target.glob("*.txt")} if target.exists() else {}

    to_add = [n for n in approved if n not in present]
    to_remove = [n for n in present if n not in approved]
    to_update = [
        n
        for n, src in approved.items()
        if n in present and present[n].read_text() != src.read_text()
    ]
    return {
        "approved": len(approved),
        "add": len(to_add),
        "update": len(to_update),
        "remove": len(to_remove),
        "drifted": bool(to_add or to_update or to_remove),
    }


def local(cfg: Config, store: Store) -> dict:
    """Everything answerable from the disk and the manifest alone."""
    pending = _pending(store, cfg)
    missing_notes = _missing_notes(store)
    stack = _stack_drift(cfg, store)
    untriaged = store.untriaged()
    proposed = store.actions(status="proposed")
    unnamed = store.unnamed_labels(cfg.speaker_min_seconds)
    finished = store.dispatches(status="done") + store.dispatches(status="failed")
    unreviewed = [d for d in finished if not d["reviewed_at"]]

    oldest_untriaged = (
        round((time.time() - min(r["started_at"] for r in untriaged)) / DAY, 1)
        if untriaged
        else None
    )
    return {
        "pending": pending,
        "pending_total": sum(i["count"] for i in pending),
        "missing_notes": missing_notes,
        "stack": stack,
        # These two are work waiting on *you*, not on the pipeline, so they are
        # reported separately and never block an "up to date" verdict.
        "untriaged": len(untriaged),
        "oldest_untriaged_days": oldest_untriaged,
        "proposed_actions": len(proposed),
        # A voice with no name is a decision only you can make, and an agent's report
        # is something only you can accept — both belong here rather than in the
        # verdict, for the same reason triage does.
        "unnamed_voices": len(unnamed),
        "unnamed_recordings": len({r["recording_id"] for r in unnamed}),
        "unreviewed_dispatches": len(unreviewed),
        "open_dispatches": len(
            store.dispatches(status="queued") + store.dispatches(status="claimed")
        ),
    }


def remote(client, store: Store) -> dict:
    """What Plaud's cloud holds that this archive has never seen.

    Cheap — one listing call, no downloads — so the console can ask on demand.
    """
    recordings = client.recordings()
    known = {r["id"] for r in store.all()}
    new = [r for r in recordings if r.id not in known]
    return {
        "checked": True,
        "cloud_total": len(recordings),
        "new": len(new),
        "newest": [
            {
                "id": r.id,
                "filename": r.filename,
                "started_iso": time.strftime("%Y-%m-%d %H:%M", r.started_at),
                "duration_min": round(r.duration_s / 60, 1),
            }
            for r in sorted(new, key=lambda r: -r.start_time_ms)[:10]
        ],
    }


def finalize(out: dict, remote_result: dict) -> dict:
    """Attach a cloud result to a local report and decide the verdict.

    Split out so the console — which caches its cloud checks — reaches the same
    verdict as the CLI without either of them owning how the cloud got checked.
    """
    out["remote"] = remote_result
    remote_new = remote_result.get("new") or 0
    # The verdict covers what the *machine* owes. Triage and action review are yours,
    # and calling the vault stale because you have reading to do would make the
    # indicator cry wolf until it got ignored.
    blocking = (
        out["pending_total"]
        + len(out["missing_notes"])
        + (1 if out["stack"]["drifted"] else 0)
        + remote_new
    )
    out["up_to_date"] = blocking == 0
    out["headline"] = headline(out, remote_new)
    return out


def report(cfg: Config, store: Store, *, client=None) -> dict:
    """The whole verdict. Pass a client to include the cloud, omit it to stay offline."""
    if client is None:
        result = {"checked": False, "detail": ""}
    else:
        try:
            result = remote(client, store)
        except Exception as exc:  # noqa: BLE001 — offline is a state, not a crash
            result = {"checked": False, "detail": str(exc)[:200]}
    return finalize(local(cfg, store), result)


def headline(out: dict, remote_new: int) -> str:
    if remote_new:
        return f"{remote_new} new recording{'s' if remote_new != 1 else ''} in Plaud's cloud to pull down"
    if out["pending_total"]:
        first = out["pending"][0]
        return f"{first['count']} recording{'s' if first['count'] != 1 else ''} not yet {first['label']}"
    if out["missing_notes"]:
        n = len(out["missing_notes"])
        return f"{n} note{'s' if n != 1 else ''} missing from the vault"
    if out["stack"]["drifted"]:
        s = out["stack"]
        return f"cognitive stack corpus out of step with triage ({s['add']}+ {s['update']}~ {s['remove']}-)"
    if not out["remote"]["checked"]:
        return "vault current as far as this disk knows — cloud not checked"
    return "vault up to date"
