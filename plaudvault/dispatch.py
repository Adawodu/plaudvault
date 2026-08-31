"""Hand an accepted action to an agent, and get the result back as a proposal.

The action board already knows what you said you would do. This is the step where you
point at one and say "you do it" — to OpenClaw, to a Claude session, to anything that
speaks MCP. The agent asks what is queued for it, claims one, does the work, and
reports back.

Three constraints shape the whole design, and every one of them exists because the
alternative is an archive of family conversations wired to something that can act:

1. **Only an accepted action can be dispatched.** `proposed` is the extractor's guess,
   and the extractor is measured to over-propose (product bible D10, B8). Requiring
   acceptance means a human read the quote before anything could act on it. There is
   no flag to skip this.
2. **Dispatch is a request, never an execution.** Nothing in plaudvault runs the work.
   It writes a row and waits. Whatever the agent can do, it could already do — this
   only tells it what you want, so the blast radius is the agent's, not the archive's.
3. **A finished job is a report, not a completion.** The agent's result lands on the
   dispatch row; closing the underlying action stays a human act. An agent that
   believes it booked a meeting and did not must not be able to tick the box itself.
"""

from __future__ import annotations

import time

from .config import Config
from .store import Store

STATUSES = ("queued", "claimed", "done", "failed", "cancelled")

# Statuses an action may be in when it is handed to an agent. `done` and `dropped` are
# closed; `proposed` has not been read by a human yet.
DISPATCHABLE = ("accepted", "in_progress")


class NotDispatchable(ValueError):
    """The action exists but is not in a state where handing it over makes sense."""


def agents(cfg: Config) -> list[str]:
    return cfg.agent_names


def check_agent(cfg: Config, agent: str) -> str:
    name = (agent or "").strip().lower()
    if not name:
        raise ValueError("agent is required")
    known = agents(cfg)
    if known and name not in known:
        raise ValueError(f"unknown agent {name!r} — configured agents: {', '.join(known)}")
    return name


def assign(cfg: Config, store: Store, action_id: int, agent: str, *,
           instructions: str = "") -> dict:
    """Queue an accepted action for an agent. Returns the new dispatch row."""
    name = check_agent(cfg, agent)
    action = store.get_action(action_id)
    if action is None:
        raise KeyError(action_id)
    if action["status"] not in DISPATCHABLE:
        raise NotDispatchable(
            f"action {action_id} is {action['status']} — accept it before assigning it"
        )
    # One live job per agent per action. Re-assigning an action already queued for
    # somebody would have two agents doing the same thing and neither knowing.
    for d in store.dispatches(action_id=action_id):
        if d["status"] in ("queued", "claimed"):
            raise NotDispatchable(
                f"action {action_id} is already {d['status']} with {d['agent']} "
                f"(dispatch {d['id']})"
            )
    did = store.add_dispatch(action_id, name, instructions=instructions)
    return dict(store.get_dispatch(did))


def brief(store: Store, row, *, cfg: Config | None = None) -> dict:
    """Everything an agent needs to act, and the evidence to check itself against.

    The quote and the recording travel with the job deliberately. An agent asked to
    "set up the meeting" with no source cannot tell a real commitment from a garbled
    one, and the whole point of D9's quote verification is that a human — or a
    model — can go back to the recording.
    """
    rec = store.get(row["recording_id"]) if row["recording_id"] else None
    out = {
        "dispatch_id": row["id"],
        "action_id": row["action_id"],
        "agent": row["agent"],
        "status": row["status"],
        "task": row["action_text"],
        "instructions": row["instructions"] or "",
        "owner": row["owner"] or "",
        "intent": row["intent"] or "",
        "due_at": row["due_at"],
        "due_iso": time.strftime("%Y-%m-%d", time.localtime(row["due_at"]))
        if row["due_at"] else None,
        "evidence": {
            "quote": row["quote"] or "",
            "recording_id": row["recording_id"],
        },
        "created_iso": time.strftime("%Y-%m-%d %H:%M", time.localtime(row["created_at"])),
        "claimed_by": row["claimed_by"],
        "result": row["result"],
        "error": row["error"],
    }
    if rec is not None:
        out["evidence"]["recording"] = rec["title"] or rec["filename"]
        out["evidence"]["recorded_iso"] = time.strftime(
            "%Y-%m-%d %H:%M", time.localtime(rec["started_at"])
        )
    return out


def queue(cfg: Config, store: Store, agent: str, *, status: str = "queued") -> list[dict]:
    name = check_agent(cfg, agent)
    return [brief(store, d) for d in store.dispatches(agent=name, status=status)]


def claim(cfg: Config, store: Store, dispatch_id: int, claimed_by: str) -> dict:
    row = store.get_dispatch(dispatch_id)
    if row is None:
        raise KeyError(dispatch_id)
    if not store.claim_dispatch(dispatch_id, claimed_by):
        raise NotDispatchable(
            f"dispatch {dispatch_id} is {row['status']}, not queued"
            + (f" (held by {row['claimed_by']})" if row["claimed_by"] else "")
        )
    # Working on it is a real state on the action too, so the board does not show a
    # commitment sitting untouched while an agent is in the middle of it.
    action = store.get_action(row["action_id"])
    if action is not None and action["status"] == "accepted":
        store.update_action(row["action_id"], status="in_progress")
    return brief(store, store.get_dispatch(dispatch_id))


def report(cfg: Config, store: Store, dispatch_id: int, *, ok: bool,
           result: str = "", error: str = "") -> dict:
    """The agent says what happened. The action stays open until a human closes it."""
    row = store.get_dispatch(dispatch_id)
    if row is None:
        raise KeyError(dispatch_id)
    if not store.finish_dispatch(dispatch_id, ok=ok, result=result, error=error):
        raise NotDispatchable(f"dispatch {dispatch_id} is {row['status']}, not claimed")
    return brief(store, store.get_dispatch(dispatch_id))


def cancel(store: Store, dispatch_id: int) -> dict:
    row = store.get_dispatch(dispatch_id)
    if row is None:
        raise KeyError(dispatch_id)
    if row["status"] in ("done", "failed"):
        raise NotDispatchable(f"dispatch {dispatch_id} already finished")
    store.update_dispatch(dispatch_id, status="cancelled", finished_at=int(time.time()))
    return brief(store, store.get_dispatch(dispatch_id))


def review(store: Store, dispatch_id: int) -> dict:
    """Mark that a human has read the agent's report. Purely a UI 'seen' flag."""
    row = store.get_dispatch(dispatch_id)
    if row is None:
        raise KeyError(dispatch_id)
    store.update_dispatch(dispatch_id, reviewed_at=int(time.time()))
    return brief(store, store.get_dispatch(dispatch_id))


def summary(cfg: Config, store: Store) -> dict:
    """Counts per agent per status, for the console header and `plaudctl status`."""
    rows = store.dispatches()
    out: dict[str, dict[str, int]] = {}
    for r in rows:
        out.setdefault(r["agent"], {s: 0 for s in STATUSES})[r["status"]] += 1
    return {
        "agents": out,
        "unreviewed": sum(
            1 for r in rows if r["status"] in ("done", "failed") and not r["reviewed_at"]
        ),
        "open": sum(1 for r in rows if r["status"] in ("queued", "claimed")),
    }
