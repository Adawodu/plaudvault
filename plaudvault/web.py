"""The console: triage recordings, work the action board, watch the measures.

Binds to 127.0.0.1 only. There is no auth because there is no remote access — this
serves family transcripts off a local drive and should never be exposed. If you ever
want it reachable elsewhere, put it behind a real identity proxy first.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import auth, freshness, llm, metrics, runlock, search, tiering, transcribe
from .api import PlaudClient
from .config import ArchiveUnavailable, load
from .store import Store
from .transcribe import transcript_paths

STATIC = Path(__file__).parent / "static"

app = FastAPI(title="Plaud Console", docs_url=None, redoc_url=None)


def _cfg():
    cfg = load()
    try:
        cfg.check_archive_available()
    except ArchiveUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    return cfg


def _store(cfg):
    return Store(cfg.db_path)


def _row(r) -> dict:
    return {k: r[k] for k in r.keys()}


def _sentiment_dto(sent) -> dict | None:
    if sent is None:
        return None
    return {
        **{k: sent[k] for k in ("valence", "energy", "label", "confidence", "spread", "model")},
        "drivers": [d for d in (sent["drivers"] or "").split("\n") if d],
        "segments": json.loads(sent["segments_json"] or "[]"),
        "scored_iso": time.strftime("%Y-%m-%d %H:%M", time.localtime(sent["scored_at"])),
    }


def _rec_dto(cfg, store, r) -> dict:
    t = store.triage_of(r["id"])
    acts = store.actions(recording_id=r["id"])
    return {
        **_row(r),
        "sentiment": _sentiment_dto(store.sentiment_of(r["id"])),
        "started_iso": time.strftime("%Y-%m-%d %H:%M", time.localtime(r["started_at"])),
        "duration_min": round((r["duration_s"] or 0) / 60, 1),
        "tier": t["tier"] if t else None,
        "marked_for_prune": bool(t["marked_for_prune"]) if t else False,
        "triage_note": (t["note"] if t else "") or "",
        "action_counts": {
            "proposed": sum(1 for a in acts if a["status"] == "proposed"),
            "open": sum(1 for a in acts if a["status"] in ("accepted", "in_progress")),
            "done": sum(1 for a in acts if a["status"] == "done"),
        },
    }


# ----------------------------------------------------------------------- recordings


@app.get("/api/recordings")
def recordings(tier: str | None = None, untriaged: bool = False, q: str = "",
               hidden: bool = False):
    cfg = _cfg()
    with _store(cfg) as store:
        # `hidden=true` is the only way to see dismissed recordings. The default is
        # quiet: the console is a workspace, and noise you already judged as noise
        # should not keep asking for attention.
        rows = (
            store.hidden() if hidden
            else store.untriaged() if untriaged
            else store.by_tier(tier) if tier
            else store.visible()
        )
        if q:
            needle = q.lower()
            rows = [r for r in rows if needle in (r["filename"] or "").lower()]
        return [_rec_dto(cfg, store, r) for r in rows]


@app.get("/api/recordings/{rec_id}")
def recording(rec_id: str):
    cfg = _cfg()
    with _store(cfg) as store:
        r = store.get(rec_id)
        if r is None:
            raise HTTPException(404, "no such recording")
        dto = _rec_dto(cfg, store, r)

        _, txt = transcript_paths(cfg, rec_id)
        dto["transcript"] = txt.read_text() if txt.exists() else ""

        sp = cfg.summary_dir / f"{rec_id}.md"
        dto["summary"] = sp.read_text() if sp.exists() else ""

        # Plaud's own transcript, kept for comparison — theirs vs ours.
        meta_path = cfg.meta_dir / f"{rec_id}.json"
        dto["has_plaud_transcript"] = meta_path.exists()

        dto["actions"] = [_row(a) for a in store.actions(recording_id=rec_id)]
        return dto


@app.get("/api/recordings/{rec_id}/audio")
def audio(rec_id: str):
    cfg = _cfg()
    with _store(cfg) as store:
        r = store.get(rec_id)
        if r is None or not r["audio_path"]:
            raise HTTPException(404, "no audio")
        p = Path(r["audio_path"])
        if not p.exists():
            raise HTTPException(404, "audio file missing from archive")
        return FileResponse(p, media_type="audio/mpeg")


@app.post("/api/recordings/{rec_id}/triage")
def triage(rec_id: str, body: dict = Body(...)):
    cfg = _cfg()
    tier = body.get("tier")
    if tier not in ("stack", "local", "exclude"):
        raise HTTPException(400, "tier must be stack, local or exclude")
    with _store(cfg) as store:
        if store.get(rec_id) is None:
            raise HTTPException(404, "no such recording")
        store.set_triage(
            rec_id,
            tier,
            marked_for_prune=bool(body.get("marked_for_prune")),
            note=str(body.get("note") or "")[:1000],
        )
        # Tiering is only real once the corpus on disk reflects it.
        report = tiering.sync(cfg, store)
        return {"ok": True, "tier": tier, "stack_sync": report}


@app.post("/api/recordings/{rec_id}/dismiss")
def dismiss(rec_id: str, body: dict = Body(default={})):
    """Take a recording out of the console without touching a byte of the archive.

    This is `exclude` with one click. The audio, its sha256 and its size/container
    facts all stay exactly as they are, so the recording remains verifiable and
    prunable later — and because triage lives in its own table that sync never
    writes to, the decision survives every future sync.
    """
    cfg = _cfg()
    restore = bool(body.get("restore"))
    with _store(cfg) as store:
        if store.get(rec_id) is None:
            raise HTTPException(404, "no such recording")
        store.set_triage(
            rec_id,
            "local" if restore else "exclude",
            marked_for_prune=False,
            note=str(body.get("note") or "")[:1000],
        )
        report = tiering.sync(cfg, store)
        return {"ok": True, "tier": "local" if restore else "exclude", "stack_sync": report}


# ----------------------------------------------------------------------- actions


@app.get("/api/actions")
def actions(status: str | None = None):
    cfg = _cfg()
    with _store(cfg) as store:
        out = []
        systems = {s["id"]: s["name"] for s in store.systems()}
        for a in store.actions(status=status):
            rec = store.get(a["recording_id"]) if a["recording_id"] else None
            out.append(
                {
                    **_row(a),
                    "system_name": systems.get(a["system_id"]),
                    "recording_name": rec["filename"] if rec else None,
                    "recording_date": time.strftime("%Y-%m-%d", time.localtime(rec["started_at"]))
                    if rec
                    else None,
                    "overdue": bool(
                        a["due_at"]
                        and a["due_at"] < time.time()
                        and a["status"] in ("accepted", "in_progress")
                    ),
                }
            )
        return out


@app.post("/api/actions")
def create_action(body: dict = Body(...)):
    cfg = _cfg()
    if not (body.get("text") or "").strip():
        raise HTTPException(400, "text is required")
    with _store(cfg) as store:
        allowed = {"recording_id", "text", "kind", "owner", "intent", "quote", "at_ms", "due_at", "system_id"}
        payload = {k: v for k, v in body.items() if k in allowed and v not in ("", None)}
        payload["status"] = "accepted" if body.get("accept") else "proposed"
        if payload["status"] == "accepted":
            payload["accepted_at"] = int(time.time())
        return {"id": store.add_action(**payload)}


@app.patch("/api/actions/{action_id}")
def update_action(action_id: int, body: dict = Body(...)):
    cfg = _cfg()
    allowed = {
        "text", "owner", "intent", "due_at", "status",
        "system_id", "outcome_score", "outcome_note",
    }
    fields = {k: v for k, v in body.items() if k in allowed}
    if "status" in fields and fields["status"] not in (
        "proposed", "accepted", "in_progress", "done", "dropped"
    ):
        raise HTTPException(400, "bad status")
    if fields.get("outcome_score") is not None:
        try:
            fields["outcome_score"] = max(1, min(5, int(fields["outcome_score"])))
        except (TypeError, ValueError):
            raise HTTPException(400, "outcome_score must be 1-5") from None
    with _store(cfg) as store:
        try:
            store.update_action(action_id, **fields)
        except KeyError:
            raise HTTPException(404, "no such action") from None
        return {"ok": True, "action": _row(store.get_action(action_id))}


@app.get("/api/actions/{action_id}/events")
def action_events(action_id: int):
    cfg = _cfg()
    with _store(cfg) as store:
        return [_row(e) for e in store.action_events(action_id)]


# ----------------------------------------------------------------------- systems


@app.get("/api/systems")
def systems():
    cfg = _cfg()
    with _store(cfg) as store:
        return metrics.systems(store)


@app.post("/api/systems")
def create_system(body: dict = Body(...)):
    cfg = _cfg()
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name is required")
    with _store(cfg) as store:
        sid = store.add_system(
            name, str(body.get("description") or ""), str(body.get("cadence") or "")
        )
        return {"id": sid}


# ----------------------------------------------------------------------- metrics


@app.get("/api/metrics")
def all_metrics():
    cfg = _cfg()
    with _store(cfg) as store:
        return metrics.summary(store)


@app.get("/api/sentiment/trend")
def sentiment_trend(
    days: int | None = Query(None, ge=1, le=3650),
    bucket: str = Query("week"),
    low_confidence: bool = False,
    excluded: bool = False,
):
    cfg = _cfg()
    with _store(cfg) as store:
        return metrics.sentiment_trend(
            store,
            days=days,
            bucket=bucket,
            include_low_confidence=low_confidence,
            include_excluded=excluded,
        )


# ----------------------------------------------------------------------- search


@app.get("/api/search")
def semantic_search(q: str = "", k: int = Query(20, ge=1, le=100), excluded: bool = False):
    cfg = _cfg()
    with _store(cfg) as store:
        stats = store.index_stats(cfg.embed_model)
        ok, why = search.available(cfg)
        if not q.strip():
            return {"query": "", "hits": [], "index": stats, "ready": ok, "detail": why}
        if not ok:
            raise HTTPException(503, f"embedding model unavailable — {why}")
        hits = search.search(cfg, store, q, k=k, include_excluded=excluded)
        return {"query": q, "hits": hits, "index": stats, "ready": True, "detail": "ok"}


# ----------------------------------------------------------------------- freshness

# One listing call per check is cheap, but the header polls this, so a short cache
# keeps an open console from hammering Plaud every few seconds.
_CLOUD_TTL = 300
_CLOUD_CACHE: dict = {"at": 0.0, "value": None}


def _cloud_check(cfg, store) -> dict:
    if _CLOUD_CACHE["value"] and time.time() - _CLOUD_CACHE["at"] < _CLOUD_TTL:
        return {**_CLOUD_CACHE["value"], "cached": True}
    try:
        with PlaudClient(auth.require_token(cfg), cfg.api_base) as client:
            value = freshness.remote(client, store)
    except Exception as exc:  # noqa: BLE001 — offline or logged out is a state
        return {"checked": False, "detail": str(exc)[:200]}
    _CLOUD_CACHE.update(at=time.time(), value=value)
    return {**value, "cached": False}


@app.get("/api/freshness")
def vault_freshness(cloud: bool = False):
    cfg = _cfg()
    with _store(cfg) as store:
        report = freshness.finalize(
            freshness.local(cfg, store),
            _cloud_check(cfg, store)
            if cloud
            else {"checked": False, "detail": ""},
        )

    last = runlock.last_run(cfg.archive_root)
    report["last_run"] = last
    report["last_run_iso"] = (
        time.strftime("%Y-%m-%d %H:%M", time.localtime(last["finished"])) if last else None
    )
    return report


@app.get("/api/status")
def status():
    cfg = load()
    try:
        cfg.check_archive_available()
        available = True
        detail = ""
    except ArchiveUnavailable as exc:
        available, detail = False, str(exc)
    out = {"archive_available": available, "detail": detail,
           "archive_root": str(cfg.archive_root), "vault": str(cfg.notes_dir)}
    if available:
        with _store(cfg) as store:
            out["counts"] = store.counts()
            out["prune_unlocked"] = (cfg.archive_root / "prune-probe-receipt.json").exists()
    return out


# ----------------------------------------------------------------------- run on demand

# A single background pipeline run, guarded so an impatient double-click can't start
# two transcription passes over the same files.
_RUN = {"active": False, "started": 0.0, "finished": 0.0, "log": [], "rc": None}
_RUN_LOCK = threading.Lock()


def _run_pipeline() -> None:
    exe = [sys.executable, "-m", "plaudvault.cli", "run"]
    try:
        proc = subprocess.Popen(
            exe, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
        )
        for line in proc.stdout:  # type: ignore[union-attr]
            line = line.rstrip()
            if line:
                _RUN["log"].append(line)
                del _RUN["log"][:-400]  # keep the tail bounded
        proc.wait()
        _RUN["rc"] = proc.returncode
    except Exception as exc:  # noqa: BLE001
        _RUN["log"].append(f"[fail] {exc}")
        _RUN["rc"] = 1
    finally:
        _RUN["active"] = False
        _RUN["finished"] = time.time()
        # A run is exactly the thing that makes a cached cloud answer wrong.
        _CLOUD_CACHE.update(at=0.0, value=None)


@app.post("/api/run")
def trigger_run():
    with _RUN_LOCK:
        if _RUN["active"]:
            raise HTTPException(409, "a run is already in progress")
        _RUN.update(active=True, started=time.time(), finished=0.0, log=[], rc=None)
    threading.Thread(target=_run_pipeline, daemon=True).start()
    return {"started": True}


@app.get("/api/run")
def run_status():
    return {
        "active": _RUN["active"],
        "started": _RUN["started"],
        "finished": _RUN["finished"],
        "rc": _RUN["rc"],
        "log": _RUN["log"][-80:],
    }


# ----------------------------------------------------------------------- settings


@app.get("/api/settings")
def settings():
    cfg = load()
    tr_ok, tr_why = transcribe.backend_status(cfg)
    llm_ok, llm_why = llm.available(cfg)
    return {
        "archive_root": str(cfg.archive_root),
        "notes_dir": str(cfg.notes_dir) if cfg.notes_dir else None,
        "stack_dir": str(tiering.stack_dir(cfg)),
        "email": cfg.email,
        "api_base": cfg.api_base,
        "transcribe": {"backend": cfg.resolved_transcribe_backend, "ok": tr_ok, "detail": tr_why},
        "llm": {
            "label": cfg.llm_label,
            "ok": llm_ok,
            "detail": llm_why,
            "local": llm.is_local(cfg),
        },
        "prune_min_age_days": cfg.prune_min_age_days,
        "summarize_min_seconds": cfg.summarize_min_seconds,
    }


# ----------------------------------------------------------------------- static

app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC / "index.html").read_text()


def serve(host: str = "127.0.0.1", port: int | None = None) -> None:
    import uvicorn

    port = port or load().web_port
    print(f"  Plaud Console -> http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="warning")
