"""Semantic search over your own transcripts, locally.

Keyword search fails on speech. You remember that someone talked about being
underpaid; the recording says "they're not exactly making it worth my while." No
substring links those, and the recording stays lost. Embeddings do link them, which is
the whole reason this exists.

Brute force on purpose. Twenty hours of audio is roughly 700 chunks, and 700 dot
products against a 768-dimension vector is under a millisecond in numpy — far below the
cost of the one network call that embeds the query. A vector database here would add a
dependency, a daemon, and an index to corrupt, in exchange for nothing measurable. If
this archive ever reaches six figures of chunks that trade changes; it is nowhere near.

Vectors are stored as raw float32 in the manifest beside everything else, so the
archive stays one directory you can copy, and the index is rebuildable from transcripts
at any time.
"""

from __future__ import annotations

import re
import time

import httpx
import numpy as np

from .config import Config
from .store import Store
from .transcribe import read_transcript

# Big enough that a chunk holds a whole thought, small enough that a hit points at a
# findable moment rather than "somewhere in these ten minutes".
CHUNK_CHARS = 1200
# Carried between chunks so a sentence straddling a boundary is still retrievable.
OVERLAP_CHARS = 200

TS_LINE = re.compile(r"^\[(\d{1,2}):(\d{2}):(\d{2})\]\s*(.*)$")


class EmbedError(RuntimeError):
    pass


def available(cfg: Config) -> tuple[bool, str]:
    """(reachable, reason). Never raises — the console asks this on every page."""
    try:
        r = httpx.get(f"{cfg.ollama_host}/api/tags", timeout=5)
        if not r.is_success:
            return False, f"Ollama returned HTTP {r.status_code}"
        names = [m.get("name", "") for m in r.json().get("models", [])]
        want = cfg.embed_model
        if not any(n == want or n.split(":")[0] == want.split(":")[0] for n in names):
            return False, f"model {want!r} not installed. Run: ollama pull {want.split(':')[0]}"
        return True, "ok"
    except Exception as exc:  # noqa: BLE001
        return False, f"cannot reach {cfg.ollama_host}: {exc}"


def chunk_transcript(text: str) -> list[dict]:
    """Split into overlapping windows, each tagged with the timestamp it starts at.

    Timestamps are what make a hit useful: the console can jump the audio player
    straight to the moment rather than making you scrub a ninety-minute recording.
    """
    lines: list[tuple[int | None, str]] = []
    for raw in text.splitlines():
        m = TS_LINE.match(raw.strip())
        if m:
            h, mnt, s, body = m.groups()
            lines.append(((int(h) * 3600 + int(mnt) * 60 + int(s)) * 1000, body))
        elif raw.strip():
            lines.append((None, raw.strip()))

    chunks: list[dict] = []
    cur: list[str] = []
    cur_len = 0
    start_ms: int | None = None
    for ms, body in lines:
        if start_ms is None:
            start_ms = ms
        cur.append(body)
        cur_len += len(body) + 1
        if cur_len >= CHUNK_CHARS:
            chunks.append({"start_ms": start_ms, "text": " ".join(cur)})
            # Re-seed with the tail so a thought spanning the split survives in both.
            tail, tail_len = [], 0
            for prev in reversed(cur):
                if tail_len >= OVERLAP_CHARS:
                    break
                tail.insert(0, prev)
                tail_len += len(prev) + 1
            cur, cur_len, start_ms = tail, tail_len, None
    if cur and " ".join(cur).strip():
        chunks.append({"start_ms": start_ms, "text": " ".join(cur)})
    return [c for c in chunks if len(c["text"].strip()) > 40]


# nomic-embed-text is trained with task-instruction prefixes and expects them: a stored
# passage is "search_document: ...", the thing you type is "search_query: ...". Omitting
# them embeds both as generic text, which still works well enough to look fine — every
# score just bunches into a narrow band, so relevant and irrelevant chunks land within a
# few hundredths of each other and the ranking below the top hit turns to noise. Models
# that don't use prefixes are unaffected, so this keys off the model name.
_PREFIXED = ("nomic-embed",)


def _prefix(cfg: Config, kind: str) -> str:
    base = cfg.embed_model.split(":")[0]
    return f"search_{kind}: " if any(base.startswith(p) for p in _PREFIXED) else ""


def embed(cfg: Config, texts: list[str], *, kind: str = "document", timeout: float = 300) -> np.ndarray:
    """Embed a batch. Returns L2-normalised float32, so cosine is a plain dot product.

    `kind` is "document" when indexing and "query" when searching; the two are not
    interchangeable for models that take task prefixes.
    """
    pre = _prefix(cfg, kind)
    resp = httpx.post(
        f"{cfg.ollama_host}/api/embed",
        json={"model": cfg.embed_model, "input": [pre + t for t in texts]},
        timeout=timeout,
    )
    if not resp.is_success:
        raise EmbedError(f"Ollama HTTP {resp.status_code}: {resp.text[:200]}")
    vectors = resp.json().get("embeddings") or []
    if len(vectors) != len(texts):
        raise EmbedError(f"asked for {len(texts)} embeddings, got {len(vectors)}")
    arr = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    return arr / np.maximum(norms, 1e-9)


def run(cfg: Config, store: Store, *, limit: int | None = None, force: bool = False) -> dict:
    """Index transcripts that have no vectors yet, or whose vectors are stale."""
    ok, why = available(cfg)
    if not ok:
        raise RuntimeError(f"embedding model unavailable — {why}")

    rows = store.all() if force else store.needing_index(cfg.embed_model)
    rows = [r for r in rows if r["transcript_path"]]
    if limit:
        rows = rows[:limit]

    stats = {"recordings": 0, "chunks": 0, "failed": 0}
    print(f"  {len(rows)} recordings to index · {cfg.embed_model}")

    for i, row in enumerate(rows, 1):
        text = read_transcript(cfg, row["id"])
        chunks = chunk_transcript(text)
        if not chunks:
            store.update(row["id"], indexed_at=int(time.time()))
            continue
        print(f"  [{i}/{len(rows)}] {row['filename'][:56]} — {len(chunks)} chunks ...", flush=True)
        try:
            # Batched, but not unboundedly: one huge request is a single point of
            # failure over a long transcript, and Ollama's own limits are undocumented.
            vectors = np.vstack(
                [embed(cfg, [c["text"] for c in chunks[j : j + 32]], kind="document")
                 for j in range(0, len(chunks), 32)]
            )
            store.set_chunks(row["id"], chunks, vectors, model=cfg.embed_model)
            store.update(row["id"], indexed_at=int(time.time()))
            stats["recordings"] += 1
            stats["chunks"] += len(chunks)
        except Exception as exc:  # noqa: BLE001
            stats["failed"] += 1
            print(f"    [fail] {exc}")

    return stats


def search(
    cfg: Config,
    store: Store,
    query: str,
    *,
    k: int = 20,
    include_excluded: bool = False,
    min_score: float = 0.0,
) -> list[dict]:
    """Nearest chunks to `query`, best first, one hit per recording-moment.

    Scores are raw cosine similarity. They are NOT probabilities and there is no
    threshold below which a result is "wrong" — this model puts most unrelated English
    text around 0.3-0.5, so a top hit at 0.55 may still be the best the archive has.
    The caller shows the number rather than hiding it behind a verdict.
    """
    if not query.strip():
        return []
    rows = store.chunks(model=cfg.embed_model, include_excluded=include_excluded)
    if not rows:
        return []

    matrix = np.frombuffer(b"".join(r["vector"] for r in rows), dtype=np.float32)
    matrix = matrix.reshape(len(rows), -1)
    q = embed(cfg, [query], kind="query")[0]
    scores = matrix @ q

    order = np.argsort(-scores)[: max(k * 3, k)]
    out: list[dict] = []
    seen_recordings: dict[str, int] = {}
    for idx in order:
        score = float(scores[idx])
        if score < min_score:
            break
        r = rows[int(idx)]
        # Cap per recording so one long rambling file can't own the whole page.
        n = seen_recordings.get(r["recording_id"], 0)
        if n >= 3:
            continue
        seen_recordings[r["recording_id"]] = n + 1
        out.append(
            {
                "recording_id": r["recording_id"],
                "filename": r["filename"],
                "started_at": r["started_at"],
                "started_iso": time.strftime("%Y-%m-%d %H:%M", time.localtime(r["started_at"])),
                "tier": r["tier"],
                "start_ms": r["start_ms"],
                "at": _hhmmss(r["start_ms"]),
                "score": round(score, 4),
                "text": r["text"],
            }
        )
        if len(out) >= k:
            break
    return out


def _hhmmss(ms: int | None) -> str:
    if ms is None:
        return ""
    s = int(ms // 1000)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"
