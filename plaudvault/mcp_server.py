"""plaudvault as a tool: what the recordings say, and what you want done about it.

Runs over stdio for any MCP client on this machine — Claude Code, Claude Desktop, an
OpenClaw agent. Two halves:

**Read.** Semantic search over your own transcripts, with a citation on every result:
recording, timestamp, tier, and the passage itself. The client's model does the
synthesis; this server does the retrieval and never paraphrases, because a paraphrase
with no timestamp is exactly the thing you cannot check.

**Act.** The queue of actions you assigned to an agent. An agent asks what is waiting
for it, claims one, does the work in its own world, and reports back. plaudvault never
executes anything.

Tier is enforced here and nowhere else (product bible §9). `mcp_tier_scope` decides
what a client may read; `exclude` is unreachable through every path regardless, and
audio is never served — the transcript is the surface.

    plaudctl mcp                     # stdio, for a client to launch
    plaudctl mcp --tiers stack       # tighter than the configured default
"""

from __future__ import annotations

import json
import time

# mcp 2.x renamed FastMCP to MCPServer. Both are supported so this server keeps
# working on whichever the client's environment has installed.
try:
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server

from . import dispatch as dispatch_mod
from . import search as search_mod
from .config import Config, load
from .store import Store
from .summarize import summary_path
from .transcribe import read_transcript

mcp = _Server("plaudvault", instructions=__doc__)

# Set by serve(); a per-invocation override of the configured tier scope so a client
# launched for one purpose can be given a narrower view than the console has.
_TIER_OVERRIDE: set[str] | None = None


def _cfg() -> Config:
    cfg = load()
    cfg.check_archive_available()
    return cfg


def _tiers(cfg: Config) -> set[str]:
    return _TIER_OVERRIDE if _TIER_OVERRIDE is not None else cfg.mcp_tiers


def _visible(cfg: Config, store: Store, tier: str | None) -> bool:
    """Is a recording at `tier` readable through this server?

    `exclude` is never readable and is not expressible in the scope — it is noise the
    owner dismissed, and the pipeline has stopped processing it anyway. An untriaged
    recording is its own category: nobody has judged it yet, which is a weaker claim
    than 'local', so it is scoped separately.
    """
    if tier == "exclude":
        return False
    return ("untriaged" if tier is None else tier) in _tiers(cfg)


def _cite(cfg: Config, row, store: Store) -> dict:
    """The identity of a recording as a client should see it."""
    t = store.triage_of(row["id"])
    speakers = [r["name"] for r in store.recording_speakers(row["id"]) if r["name"]]
    return {
        "recording_id": row["id"],
        "title": row["title"] or row["filename"],
        "titled_by": row["title_source"],
        "recorded": time.strftime("%Y-%m-%d %H:%M", time.localtime(row["started_at"])),
        "duration_min": round((row["duration_s"] or 0) / 60, 1),
        "tier": (t["tier"] if t else None) or "untriaged",
        "speakers": speakers,
    }


# --------------------------------------------------------------------------- read


@mcp.tool()
def search_recordings(query: str, limit: int = 8) -> str:
    """Search the voice-recording archive by meaning and return cited passages.

    Use this to answer "what did I say about X", "when did we discuss Y", "what did
    <person> commit to". Every hit carries the recording, a timestamp inside it, and
    the passage verbatim — quote the passage and cite the recording and timestamp
    rather than summarising without one.

    Scores are cosine similarity, not confidence: unrelated English sits around
    0.3-0.5, so a top hit at 0.55 may still be the best the archive holds. Compare
    scores to each other, never against an absolute bar.
    """
    cfg = _cfg()
    with Store(cfg.db_path) as store:
        ok, why = search_mod.available(cfg)
        if not ok:
            return json.dumps({"error": f"embedding model unavailable — {why}"})
        # Over-fetch, because tier filtering happens after ranking and could
        # otherwise return three hits when the caller asked for eight.
        hits = search_mod.search(cfg, store, query, k=max(limit * 3, limit))
        out = []
        for h in hits:
            if not _visible(cfg, store, h["tier"]):
                continue
            row = store.get(h["recording_id"])
            out.append(
                {
                    **_cite(cfg, row, store),
                    "at": h["at"],
                    "score": h["score"],
                    "passage": h["text"],
                }
            )
            if len(out) >= limit:
                break
        return json.dumps(
            {
                "query": query,
                "hits": out,
                "scope": sorted(_tiers(cfg)),
                "note": "scores are cosine similarity, not confidence",
            },
            indent=1,
        )


@mcp.tool()
def get_recording(recording_id: str, include_transcript: bool = False) -> str:
    """Everything known about one recording: summary, speakers, actions, tone.

    Set `include_transcript` only when the summary is not enough — a transcript can
    be tens of thousands of characters. Prefer `get_transcript` with a time window.
    """
    cfg = _cfg()
    with Store(cfg.db_path) as store:
        row = store.get(recording_id)
        if row is None:
            return json.dumps({"error": "no such recording"})
        t = store.triage_of(recording_id)
        if not _visible(cfg, store, t["tier"] if t else None):
            return json.dumps({"error": "recording is outside this client's tier scope"})

        sp = summary_path(cfg, recording_id)
        sent = store.sentiment_of(recording_id)
        out = {
            **_cite(cfg, row, store),
            "summary": sp.read_text() if sp.exists() else "",
            "speakers": [
                {
                    "label": r["label"],
                    "name": r["name"],
                    "identified_by": r["source"],
                    "speaking_minutes": round((r["seconds"] or 0) / 60, 1),
                    "contact_ref": r["external_ref"],
                }
                for r in store.recording_speakers(recording_id)
            ],
            "tone": {
                "valence": sent["valence"],
                "label": sent["label"],
                "confidence": sent["confidence"],
                "caveat": "estimated by a language model reading ASR — no tone of voice",
            }
            if sent else None,
            "actions": [
                {"id": a["id"], "text": a["text"], "status": a["status"],
                 "owner": a["owner"], "quote": a["quote"]}
                for a in store.actions(recording_id=recording_id)
            ],
        }
        if include_transcript:
            out["transcript"] = read_transcript(cfg, recording_id)
        return json.dumps(out, indent=1)


@mcp.tool()
def get_transcript(recording_id: str, from_time: str = "", to_time: str = "") -> str:
    """A transcript, optionally just the window you need.

    `from_time` and `to_time` are HH:MM:SS as they appear in search results, so a hit
    at 00:12:30 can be opened as 00:11:00 to 00:15:00 to read around it.
    """
    cfg = _cfg()
    with Store(cfg.db_path) as store:
        row = store.get(recording_id)
        if row is None:
            return json.dumps({"error": "no such recording"})
        t = store.triage_of(recording_id)
        if not _visible(cfg, store, t["tier"] if t else None):
            return json.dumps({"error": "recording is outside this client's tier scope"})

        def seconds(stamp: str) -> int | None:
            parts = [p for p in stamp.strip().split(":") if p.strip().isdigit()]
            if not parts:
                return None
            total = 0
            for p in parts:
                total = total * 60 + int(p)
            return total

        lo, hi = seconds(from_time), seconds(to_time)
        lines = read_transcript(cfg, recording_id).splitlines()
        if lo is not None or hi is not None:
            kept = []
            for ln in lines:
                at = seconds(ln[1:9]) if ln.startswith("[") else None
                if at is None:
                    continue
                if lo is not None and at < lo:
                    continue
                if hi is not None and at > hi:
                    break
                kept.append(ln)
            lines = kept
        return json.dumps(
            {**_cite(cfg, row, store), "window": [from_time, to_time],
             "transcript": "\n".join(lines)},
            indent=1,
        )


@mcp.tool()
def list_recordings(limit: int = 20, speaker: str = "", since: str = "") -> str:
    """Recent recordings, newest first. Filter by a speaker's name or a start date.

    `since` is YYYY-MM-DD. `speaker` matches a named person, so "which recordings has    this person been in" is answerable without reading anything.
    """
    cfg = _cfg()
    with Store(cfg.db_path) as store:
        cutoff = None
        if since.strip():
            try:
                cutoff = int(time.mktime(time.strptime(since.strip(), "%Y-%m-%d")))
            except ValueError:
                return json.dumps({"error": "since must be YYYY-MM-DD"})

        out = []
        for row in store.visible():
            t = store.triage_of(row["id"])
            if not _visible(cfg, store, t["tier"] if t else None):
                continue
            if cutoff and row["started_at"] < cutoff:
                continue
            cite = _cite(cfg, row, store)
            if speaker.strip() and not any(
                speaker.strip().lower() in (s or "").lower() for s in cite["speakers"]
            ):
                continue
            out.append(cite)
            if len(out) >= max(1, min(limit, 200)):
                break
        return json.dumps({"recordings": out, "scope": sorted(_tiers(cfg))}, indent=1)


@mcp.tool()
def list_speakers() -> str:
    """People the archive can recognise by voice, with their contact references.

    `contact_ref` is an opaque id the owner attached — a CRM record, a contact card.
    Use it to join a voice to whatever system holds the rest of that relationship.
    """
    cfg = _cfg()
    with Store(cfg.db_path) as store:
        return json.dumps(
            {
                "speakers": [
                    {
                        "name": s["name"],
                        "is_owner": bool(s["is_me"]),
                        "contact_ref": s["external_ref"],
                        "recordings": s["recordings"],
                        "has_voiceprint": bool(s["voiceprint"]),
                    }
                    for s in store.speakers()
                ]
            },
            indent=1,
        )


# --------------------------------------------------------------------------- act


@mcp.tool()
def my_tasks(agent: str, status: str = "queued") -> str:
    """Actions the owner assigned to you. Start here before doing any work.

    Each task carries the commitment, the owner's extra instructions, and the quote
    from the recording it came from — check the task against that quote before acting,
    and say so if they disagree rather than proceeding.
    """
    cfg = _cfg()
    with Store(cfg.db_path) as store:
        try:
            tasks = dispatch_mod.queue(cfg, store, agent, status=status)
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        return json.dumps({"agent": agent.lower(), "status": status, "tasks": tasks}, indent=1)


@mcp.tool()
def claim_task(dispatch_id: int, claimed_by: str = "") -> str:
    """Take a queued task so no other agent starts the same work.

    Claim before acting, not after. A claim fails if somebody already holds it, and a
    failed claim means stop — do not do the work anyway.
    """
    cfg = _cfg()
    with Store(cfg.db_path) as store:
        try:
            return json.dumps(
                dispatch_mod.claim(cfg, store, dispatch_id, claimed_by or "mcp-client"),
                indent=1,
            )
        except KeyError:
            return json.dumps({"error": "no such dispatch"})
        except dispatch_mod.NotDispatchable as exc:
            return json.dumps({"error": str(exc)})


@mcp.tool()
def report_task(dispatch_id: int, ok: bool, result: str = "", error: str = "") -> str:
    """Report what you did. This does NOT close the action — a human still reviews it.

    Write `result` as what actually happened and what the owner should check: what you
    sent, to whom, what is now on a calendar, what you could not do. If you could not
    complete it, pass ok=false with the reason rather than a partial success.
    """
    cfg = _cfg()
    with Store(cfg.db_path) as store:
        try:
            return json.dumps(
                dispatch_mod.report(cfg, store, dispatch_id, ok=ok, result=result,
                                    error=error),
                indent=1,
            )
        except KeyError:
            return json.dumps({"error": "no such dispatch"})
        except dispatch_mod.NotDispatchable as exc:
            return json.dumps({"error": str(exc)})


@mcp.tool()
def propose_action(text: str, recording_id: str = "", owner: str = "",
                   quote: str = "") -> str:
    """Put a commitment you noticed onto the owner's board as a PROPOSAL.

    It lands unaccepted and does nothing until a human accepts it — the same status
    the archive's own extractor writes into. Use it when a conversation you were given
    contains something the extractor missed; do not use it to record your own plans.
    """
    cfg = _cfg()
    if not text.strip():
        return json.dumps({"error": "text is required"})
    with Store(cfg.db_path) as store:
        if recording_id and store.get(recording_id) is None:
            return json.dumps({"error": "no such recording"})
        aid = store.add_action(
            recording_id=recording_id or None,
            text=text.strip()[:500],
            kind="manual",
            owner=owner.strip()[:100],
            quote=quote.strip()[:500],
            status="proposed",
        )
        return json.dumps({"action_id": aid, "status": "proposed",
                           "note": "awaiting human acceptance"}, indent=1)


@mcp.tool()
def list_actions(status: str = "accepted", limit: int = 30) -> str:
    """The action board: proposed | accepted | in_progress | done | dropped."""
    cfg = _cfg()
    with Store(cfg.db_path) as store:
        out = []
        for a in store.actions(status=status or None):
            rec = store.get(a["recording_id"]) if a["recording_id"] else None
            if rec is not None:
                t = store.triage_of(rec["id"])
                if not _visible(cfg, store, t["tier"] if t else None):
                    continue
            out.append(
                {
                    "action_id": a["id"],
                    "text": a["text"],
                    "status": a["status"],
                    "owner": a["owner"],
                    "due_iso": time.strftime("%Y-%m-%d", time.localtime(a["due_at"]))
                    if a["due_at"] else None,
                    "recording": (rec["title"] or rec["filename"]) if rec else None,
                    "recording_id": a["recording_id"],
                    "dispatched": [
                        {"dispatch_id": d["id"], "agent": d["agent"], "status": d["status"]}
                        for d in store.dispatches(action_id=a["id"])
                    ],
                }
            )
            if len(out) >= max(1, min(limit, 200)):
                break
        return json.dumps({"status": status, "actions": out}, indent=1)


def serve(tiers: set[str] | None = None) -> None:
    global _TIER_OVERRIDE
    _TIER_OVERRIDE = tiers
    mcp.run()
