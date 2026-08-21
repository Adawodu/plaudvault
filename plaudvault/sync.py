"""Pull recordings + Plaud's own metadata down to local disk, and verify them.

Downloads are atomic (temp file, fsync, rename) and integrity-checked against the
md5 Plaud publishes in the listing. Nothing downstream — and especially not
pruning — trusts a file that has not been verified here.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from .api import PlaudClient, Recording
from .config import Config
from .store import Store

# Plaud prepends an ID3 tag (observed 512-640 bytes). Allow generous headroom for
# tag growth while still catching a truncated download, which reads as negative.
MAX_TAG_BYTES = 65536


def audio_kind(path: Path) -> str | None:
    """Identify the container by magic bytes, or None if we can't decode it.

    Plaud serves two different things from the same endpoint. Once a recording has
    been transcoded you get a real MP3. Before that, the `.opus` URL returns the raw
    on-device blob (starts with 0xB8 0x60), which is not Ogg/Opus and which no
    decoder will touch — and whose md5 matches Plaud's `file_md5` exactly, because it
    IS the original. So "md5 matched" and "usable audio" are unrelated properties,
    and only this check tells them apart.
    """
    with path.open("rb") as fh:
        head = fh.read(12)
    if head[:3] == b"ID3" or head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2", b"\xff\xfa"):
        return "mp3"
    if head[:4] == b"OggS":
        return "ogg"
    if head[:4] == b"RIFF":
        return "wav"
    if head[:4] == b"fLaC":
        return "flac"
    if head[4:8] == b"ftyp":
        return "mp4"
    return None


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def md5_file(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def audio_path(cfg: Config, rec: Recording) -> Path:
    t = rec.started_at
    ext = "opus" if cfg.prefer_opus else "mp3"
    return cfg.audio_dir / f"{t.tm_year:04d}" / f"{t.tm_mon:02d}" / f"{rec.id}.{ext}"


def sync(cfg: Config, client: PlaudClient, store: Store, *, limit: int | None = None) -> dict:
    """Download every not-yet-archived recording. Idempotent and resumable."""
    cfg.ensure_dirs()
    recordings = client.recordings()
    print(f"  {len(recordings)} recordings in Plaud cloud")

    stats = {"new": 0, "skipped": 0, "failed": 0, "bytes": 0, "unverified": 0, "undecodable": 0}
    todo = []
    for rec in recordings:
        store.upsert_remote(rec)
        row = store.get(rec.id)
        dest = audio_path(cfg, rec)
        if row and row["audio_sha256"] and dest.exists() and row["audio_kind"]:
            stats["skipped"] += 1
            continue
        todo.append(rec)

    if limit:
        todo = todo[:limit]
    print(f"  {len(todo)} to download")

    for i, rec in enumerate(todo, 1):
        dest = audio_path(cfg, rec)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        label = f"[{i}/{len(todo)}] {rec.filename[:60]}"
        try:
            print(f"  {label} ...", flush=True)
            client.download(rec.id, tmp, opus=cfg.prefer_opus)

            with tmp.open("rb") as fh:
                os.fsync(fh.fileno())

            digest = sha256_file(tmp)
            size = tmp.stat().st_size

            # Plaud's `file_md5`/`filesize` describe the ORIGINAL on-device file. The
            # MP3 their storage layer serves usually carries a 512-640 byte ID3 tag on
            # top, so the md5 matches only for the minority of files served untouched.
            # Treating a mismatch as corruption would condemn most of the archive to
            # never being prunable, so completeness is judged on size instead:
            # a download that is short is truncated; a download that is slightly long
            # is tagged. Bitrot after the fact is caught by `verify` re-hashing sha256.
            delta = size - (rec.filesize or 0)
            size_ok = rec.filesize > 0 and 0 <= delta <= MAX_TAG_BYTES
            md5_exact = bool(rec.file_md5) and md5_file(tmp) == rec.file_md5

            kind = audio_kind(tmp)
            if kind is None:
                # Plaud has not finished transcoding this one. Keep the bytes (they are
                # the only copy we have) but refuse to call it archived: it stays
                # un-prunable and the next sync will try again for a real MP3.
                stats["undecodable"] += 1
                print("    [warn] not decodable audio yet (Plaud still transcoding) — will retry")

            if not size_ok:
                stats["unverified"] += 1
                if delta < 0:
                    print(f"    [warn] short by {-delta} bytes — truncated download, not prunable")
                else:
                    print(f"    [warn] {delta} bytes larger than expected — not prunable")

            tmp.rename(dest)

            # Plaud's own transcript/summary, saved verbatim as provenance. We do not
            # use it downstream — it exists so the archive records what their AI said.
            meta = {"recording": rec.raw}
            try:
                meta["detail"] = client.file_detail(rec.id)
            except Exception as exc:  # noqa: BLE001
                meta["detail_error"] = str(exc)
            meta_path = cfg.meta_dir / f"{rec.id}.json"
            meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False))

            store.update(
                rec.id,
                audio_path=str(dest),
                audio_sha256=digest,
                audio_size=size,
                size_ok=1 if size_ok else 0,
                audio_kind=kind,
                md5_verified=1 if md5_exact else 0,
                downloaded_at=int(time.time()),
            )
            stats["new"] += 1
            stats["bytes"] += size
        except Exception as exc:  # noqa: BLE001
            tmp.unlink(missing_ok=True)
            stats["failed"] += 1
            print(f"    [fail] {exc}")

    return stats


def verify(cfg: Config, store: Store) -> dict:
    """Re-hash every archived file and flag drift. Run before any prune."""
    stats = {"ok": 0, "missing": 0, "corrupt": 0, "undecodable": 0}
    for row in store.all():
        if not row["audio_path"] or not row["audio_sha256"]:
            continue
        p = Path(row["audio_path"])
        if not p.exists():
            stats["missing"] += 1
            print(f"  [missing] {row['filename']} -> {p}")
            store.update(row["id"], size_ok=0)
            continue
        if sha256_file(p) != row["audio_sha256"]:
            stats["corrupt"] += 1
            print(f"  [corrupt] {row['filename']} -> {p}")
            store.update(row["id"], size_ok=0)
            continue
        if audio_kind(p) is None:
            stats["undecodable"] += 1
            print(f"  [undecodable] {row['filename']} — re-sync to get the transcoded version")
            store.update(row["id"], audio_kind=None)
            continue
        stats["ok"] += 1
    return stats
