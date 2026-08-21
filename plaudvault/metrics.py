"""The measurement layer.

Everything here is derived from recorded facts — the append-only `action_events`
journal and timestamps — never from self-reported progress. Where there isn't
enough data to say something honest, these functions return `None` rather than a
flattering zero.
"""

from __future__ import annotations

import statistics
import time
from typing import Any

from .store import Store

DAY = 86400


def _median_days(deltas: list[float]) -> float | None:
    return round(statistics.median(deltas) / DAY, 1) if deltas else None


def completion(store: Store, *, window_days: int | None = None) -> dict[str, Any]:
    """Did extracted commitments actually get done, and how long did they take?"""
    rows = store.actions()
    if window_days:
        cutoff = time.time() - window_days * DAY
        rows = [r for r in rows if r["created_at"] >= cutoff]

    decided = [r for r in rows if r["status"] != "proposed"]
    accepted = [r for r in rows if r["status"] in ("accepted", "in_progress", "done")]
    done = [r for r in rows if r["status"] == "done"]
    dropped = [r for r in rows if r["status"] == "dropped"]

    # Cycle time = accepted -> done. Only meaningful for actions that have both.
    cycle = [
        r["completed_at"] - r["accepted_at"]
        for r in done
        if r["completed_at"] and r["accepted_at"]
    ]
    # Time spent sitting as a proposal before anyone decided.
    triage_lag = [
        r["accepted_at"] - r["created_at"] for r in accepted if r["accepted_at"]
    ]

    now = time.time()
    overdue = [
        r for r in rows
        if r["due_at"] and r["due_at"] < now and r["status"] in ("accepted", "in_progress")
    ]

    return {
        "total": len(rows),
        "proposed": len(rows) - len(decided),
        "accepted": len(accepted),
        "done": len(done),
        "dropped": len(dropped),
        "overdue": len(overdue),
        # Of the actions you committed to, what share did you finish?
        "completion_rate": round(len(done) / len(accepted), 2) if accepted else None,
        # Of everything the extractor proposed, what share did you accept?
        "acceptance_rate": round(len(accepted) / len(decided), 2) if decided else None,
        "median_cycle_days": _median_days(cycle),
        "median_decision_days": _median_days(triage_lag),
    }


def outcomes(store: Store) -> dict[str, Any]:
    """Did completed actions produce what they were supposed to?

    Scored 1-5 against the `intent` stated at accept time. Completing an action
    is not the same as it having worked, and this is the difference.
    """
    done = [r for r in store.actions(status="done")]
    scored = [r for r in done if r["outcome_score"] is not None]
    with_intent = [r for r in done if (r["intent"] or "").strip()]

    dist = {n: 0 for n in range(1, 6)}
    for r in scored:
        dist[int(r["outcome_score"])] = dist.get(int(r["outcome_score"]), 0) + 1

    return {
        "done": len(done),
        "scored": len(scored),
        "unscored": len(done) - len(scored),
        # How much of your completed work can even be judged — no intent, no verdict.
        "intent_coverage": round(len(with_intent) / len(done), 2) if done else None,
        "mean_outcome": round(statistics.mean([r["outcome_score"] for r in scored]), 2)
        if scored
        else None,
        "distribution": dist,
        # Completed but ineffective: the number worth actually looking at.
        "low_outcome": [
            {"id": r["id"], "text": r["text"], "intent": r["intent"], "score": r["outcome_score"]}
            for r in scored
            if r["outcome_score"] <= 2
        ],
    }


def systems(store: Store) -> list[dict[str, Any]]:
    """Adherence per named system: of its actions, what share got done, and lately?"""
    out = []
    for sys_row in store.systems():
        rows = [a for a in store.actions() if a["system_id"] == sys_row["id"]]
        done = [a for a in rows if a["status"] == "done"]
        live = [a for a in rows if a["status"] in ("accepted", "in_progress", "done")]
        recent_cut = time.time() - 30 * DAY
        recent = [a for a in rows if a["created_at"] >= recent_cut]
        recent_done = [a for a in recent if a["status"] == "done"]
        scored = [a["outcome_score"] for a in done if a["outcome_score"] is not None]
        out.append(
            {
                "id": sys_row["id"],
                "name": sys_row["name"],
                "cadence": sys_row["cadence"],
                "description": sys_row["description"],
                "instances": len(rows),
                "done": len(done),
                "adherence": round(len(done) / len(live), 2) if live else None,
                "adherence_30d": round(len(recent_done) / len(recent), 2) if recent else None,
                "mean_outcome": round(statistics.mean(scored), 2) if scored else None,
            }
        )
    return sorted(out, key=lambda s: -s["instances"])


def pipeline(store: Store) -> dict[str, Any]:
    """Is the capture pipeline earning its keep, or just accumulating audio?"""
    recs = store.all()
    transcribed = [r for r in recs if r["transcript_path"]]
    triaged_ids = {
        r["recording_id"]
        for r in store.db.execute("SELECT recording_id FROM triage").fetchall()
    }
    triaged = [r for r in transcribed if r["id"] in triaged_ids]

    lag = []
    for r in triaged:
        t = store.triage_of(r["id"])
        if t and t["decided_at"] and r["started_at"]:
            lag.append(t["decided_at"] - r["started_at"])

    with_actions = {
        r["recording_id"] for r in store.db.execute(
            "SELECT DISTINCT recording_id FROM actions WHERE status != 'proposed'"
        ).fetchall()
    }
    hours = sum(r["duration_s"] or 0 for r in recs) / 3600
    action_count = len(store.actions())

    return {
        "recordings": len(recs),
        "transcribed": len(transcribed),
        "triaged": len(triaged),
        "untriaged": len(transcribed) - len(triaged),
        "median_decision_days": _median_days(lag),
        # The blunt question: what share of what you recorded became anything?
        "conversion_rate": round(len(with_actions) / len(transcribed), 2)
        if transcribed
        else None,
        "audio_hours": round(hours, 1),
        "actions_per_hour": round(action_count / hours, 2) if hours else None,
        "oldest_untriaged_days": round(
            (time.time() - min((r["started_at"] for r in transcribed
                                if r["id"] not in triaged_ids), default=time.time())) / DAY, 1
        ) if len(transcribed) > len(triaged) else None,
    }


# ---------------------------------------------------------------------- sentiment

# Readings the model itself was unsure of still get plotted as points, but are kept
# out of the trend line by default. A confident-looking average built from readings
# the model flagged as guesses is worse than a gap in the line.
CONFIDENCE_FLOOR = 0.4


def _bucket_start(ts: float, bucket: str) -> int:
    """Local-time start of the day/week/month containing `ts`.

    Re-normalised through mktime with isdst=-1 so a week boundary that crosses a DST
    change still lands on local midnight rather than drifting an hour.
    """
    lt = time.localtime(ts)
    if bucket == "day":
        y, m, d = lt.tm_year, lt.tm_mon, lt.tm_mday
    elif bucket == "month":
        y, m, d = lt.tm_year, lt.tm_mon, 1
    else:  # week, starting Monday
        monday = time.localtime(ts - lt.tm_wday * DAY)
        y, m, d = monday.tm_year, monday.tm_mon, monday.tm_mday
    return int(time.mktime((y, m, d, 0, 0, 0, 0, 0, -1)))


def sentiment_trend(
    store: Store,
    *,
    days: int | None = None,
    bucket: str = "week",
    include_low_confidence: bool = False,
    include_excluded: bool = False,
) -> dict[str, Any]:
    """Tone over time: one point per recording, plus a bucketed mean.

    Bucket means are weighted by recording length, because an hour-long conversation
    carries more of a week's emotional weight than a two-minute voice memo, and an
    unweighted mean lets the memo outvote it.

    Recordings tiered `exclude` are left out by default. Tiering is physical
    everywhere else in this project, and a chart that keeps plotting what you
    explicitly excluded — the vendor's own demo files, misfires, empty rooms —
    quietly reports their mood as yours.
    """
    bucket = bucket if bucket in ("day", "week", "month") else "week"
    since = int(time.time() - days * DAY) if days else None
    rows = store.sentiment_series(since=since)
    excluded = sum(1 for r in rows if r["tier"] == "exclude")
    if not include_excluded:
        rows = [r for r in rows if r["tier"] != "exclude"]

    points = [
        {
            "id": r["recording_id"],
            "filename": r["filename"],
            "started_at": r["started_at"],
            "started_iso": time.strftime("%Y-%m-%d %H:%M", time.localtime(r["started_at"])),
            "duration_min": round((r["duration_s"] or 0) / 60, 1),
            "valence": r["valence"],
            "energy": r["energy"],
            "label": r["label"],
            "confidence": r["confidence"],
            "spread": r["spread"],
            "drivers": [d for d in (r["drivers"] or "").split("\n") if d],
            "tier": r["tier"],
            "low_confidence": (r["confidence"] or 0) < CONFIDENCE_FLOOR,
        }
        for r in rows
    ]

    counted = [p for p in points if include_low_confidence or not p["low_confidence"]]

    groups: dict[int, list[dict]] = {}
    for p in counted:
        groups.setdefault(_bucket_start(p["started_at"], bucket), []).append(p)

    buckets = []
    for start in sorted(groups):
        members = groups[start]
        weights = [max(m["duration_min"], 1.0) for m in members]
        total = sum(weights)
        buckets.append(
            {
                "start": start,
                "start_iso": time.strftime("%Y-%m-%d", time.localtime(start)),
                "n": len(members),
                "mean_valence": round(
                    sum(m["valence"] * w for m, w in zip(members, weights)) / total, 3
                ),
                "mean_energy": round(
                    sum((m["energy"] or 0) * w for m, w in zip(members, weights)) / total, 3
                ),
                "hours": round(sum(weights) / 60, 1),
            }
        )

    # Direction of travel: this half of the window against the previous half. Only
    # stated when both halves have something in them.
    delta = None
    if len(buckets) >= 4:
        half = len(buckets) // 2
        earlier = statistics.mean(b["mean_valence"] for b in buckets[:half])
        later = statistics.mean(b["mean_valence"] for b in buckets[half:])
        delta = round(later - earlier, 3)

    scored = store.db.execute("SELECT COUNT(*) FROM sentiment").fetchone()[0]
    transcribed = store.db.execute(
        "SELECT COUNT(*) FROM recordings WHERE transcript_path IS NOT NULL"
    ).fetchone()[0]

    return {
        "bucket": bucket,
        "days": days,
        "confidence_floor": CONFIDENCE_FLOOR,
        "include_low_confidence": include_low_confidence,
        "include_excluded": include_excluded,
        "excluded_by_tier": excluded,
        "points": points,
        "buckets": buckets,
        "counted": len(counted),
        "excluded_low_confidence": len(points) - len(counted),
        "mean_valence": round(statistics.mean([p["valence"] for p in counted]), 3)
        if counted
        else None,
        "mean_energy": round(
            statistics.mean([p["energy"] for p in counted if p["energy"] is not None]), 3
        )
        if any(p["energy"] is not None for p in counted)
        else None,
        "delta": delta,
        "distribution": {
            lab: sum(1 for p in counted if p["label"] == lab)
            for lab in ("positive", "neutral", "mixed", "negative")
        },
        "coverage": {
            "scored": scored,
            "transcribed": transcribed,
            "rate": round(scored / transcribed, 2) if transcribed else None,
        },
    }


def summary(store: Store) -> dict[str, Any]:
    return {
        "completion": completion(store),
        "outcomes": outcomes(store),
        "systems": systems(store),
        "pipeline": pipeline(store),
    }
