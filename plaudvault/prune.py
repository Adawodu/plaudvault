"""Remove archived recordings from Plaud's cloud — the one destructive verb.

Design stance: this deletes data from a service that is currently your only copy's
upstream, using an endpoint whose exact shape is INFERRED rather than documented.
So it is paranoid on purpose:

  1. Dry-run is the default. `--yes` is required to send anything.
  2. Bulk pruning is refused until `--probe` has trashed exactly one recording and
     confirmed via the API that it actually landed in the trash. The proof is written
     to a receipt file; delete the receipt and you are back to probe-only.
  3. A recording is eligible only if it is md5-verified, re-hashed clean at prune
     time, transcribed, noted, and older than `prune_min_age_days`.
  4. `trash` is used, not hard delete — recoverable from the Plaud app for a window.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .api import PlaudClient
from .config import Config
from .store import Store
from .sync import sha256_file

RECEIPT_NAME = "prune-probe-receipt.json"


def receipt_path(cfg: Config) -> Path:
    return cfg.archive_root / RECEIPT_NAME


def _eligible(cfg: Config, row) -> tuple[bool, str]:
    """Re-check every precondition at prune time, not just at query time."""
    if not row["audio_path"]:
        return False, "no local audio"
    p = Path(row["audio_path"])
    if not p.exists():
        return False, "local audio missing"
    if not row["size_ok"]:
        return False, "download never confirmed complete"
    if sha256_file(p) != row["audio_sha256"]:
        return False, "local audio hash drifted"
    if not row["transcript_path"] or not Path(row["transcript_path"]).exists():
        return False, "no transcript"
    if cfg.notes_dir is not None and (
        not row["note_path"] or not Path(row["note_path"]).exists()
    ):
        return False, "no vault note"
    age_days = (time.time() - row["started_at"]) / 86400
    if age_days < cfg.prune_min_age_days:
        return False, f"only {age_days:.0f}d old (floor is {cfg.prune_min_age_days}d)"
    return True, ""


def _marked(store: Store, rec_id: str) -> bool:
    """Cloud deletion requires an explicit mark from the console. Absence is a no."""
    t = store.triage_of(rec_id)
    return bool(t and t["marked_for_prune"])


def probe(cfg: Config, client: PlaudClient, store: Store, *, confirm: bool) -> bool:
    """Trash exactly one eligible recording and verify it moved. Writes a receipt."""
    candidates = store.prunable(cfg.prune_min_age_days, require_note=cfg.notes_dir is not None)
    target = None
    for row in candidates:
        if not _marked(store, row["id"]):
            continue
        ok, _ = _eligible(cfg, row)
        if ok:
            target = row
            break
    if target is None:
        print("  no recording is both marked-for-deletion in the console and fully verified.")
        print("  Mark one in the web UI (plaudctl web) first — probing picks from that set.")
        return False

    when = time.strftime("%Y-%m-%d", time.localtime(target["started_at"]))
    print(f"  probe target: {target['filename'][:60]} ({when})")
    print(f"    local audio: {target['audio_path']}")
    print(f"    transcript:  {target['transcript_path']}")
    print(f"    vault note:  {target['note_path']}")
    if not confirm:
        print("\n  DRY RUN — nothing sent. Re-run with --yes to trash this one recording.")
        return False

    print("  sending trash request ...")
    resp = client.trash(target["id"])
    print(f"    response: {json.dumps(resp)[:200]}")

    time.sleep(2)
    trashed_ids = {r.id for r in client.recordings(is_trash=1)}
    live_ids = {r.id for r in client.recordings(is_trash=0)}

    if target["id"] in trashed_ids and target["id"] not in live_ids:
        receipt_path(cfg).write_text(
            json.dumps(
                {
                    "verified_at": int(time.time()),
                    "file_id": target["id"],
                    "filename": target["filename"],
                    "method": "PATCH /file/{id} {is_trash: true}",
                    "api_response": resp,
                },
                indent=2,
            )
        )
        store.update(target["id"], pruned_at=int(time.time()))
        print("\n  VERIFIED: recording moved to Plaud trash. Bulk prune is now unlocked.")
        return True

    print("\n  NOT VERIFIED: the recording did not move to trash.")
    print("  The inferred endpoint is wrong or a no-op. Bulk prune stays locked.")
    print(f"    in trash list: {target['id'] in trashed_ids}")
    print(f"    still live:    {target['id'] in live_ids}")
    return False


def run(cfg: Config, client: PlaudClient, store: Store, *, confirm: bool, limit: int | None) -> dict:
    stats = {"pruned": 0, "skipped": 0, "failed": 0}

    if not receipt_path(cfg).exists():
        print("  BLOCKED: no probe receipt.")
        print("  The Plaud trash endpoint is inferred, not documented. Verify it on one")
        print("  recording first:  plaudctl prune --probe --yes")
        return stats

    rows = store.prunable(cfg.prune_min_age_days, require_note=cfg.notes_dir is not None)
    eligible = []
    for row in rows:
        if not _marked(store, row["id"]):
            stats["skipped"] += 1
            continue  # not marked in the console — silently left alone, as intended
        ok, reason = _eligible(cfg, row)
        if ok:
            eligible.append(row)
        else:
            stats["skipped"] += 1
            print(f"  [skip] {row['filename'][:50]}: {reason}")

    if not eligible:
        print("  nothing marked for deletion in the console.")
        print("  Mark recordings in the web UI (plaudctl web) before pruning.")

    if limit:
        eligible = eligible[:limit]

    total_bytes = sum(r["audio_size"] or 0 for r in eligible)
    print(f"\n  {len(eligible)} recordings eligible ({total_bytes / 1e9:.2f} GB archived locally)")

    if not confirm:
        for row in eligible[:20]:
            when = time.strftime("%Y-%m-%d", time.localtime(row["started_at"]))
            print(f"    would trash: {when}  {row['filename'][:60]}")
        if len(eligible) > 20:
            print(f"    ... and {len(eligible) - 20} more")
        print("\n  DRY RUN — nothing sent. Re-run with --yes to trash these.")
        return stats

    for i, row in enumerate(eligible, 1):
        try:
            client.trash(row["id"])
            store.update(row["id"], pruned_at=int(time.time()))
            stats["pruned"] += 1
            print(f"  [{i}/{len(eligible)}] trashed {row['filename'][:60]}")
            time.sleep(0.3)  # be polite to their API
        except Exception as exc:  # noqa: BLE001
            stats["failed"] += 1
            print(f"  [fail] {row['filename'][:50]}: {exc}")

    return stats
