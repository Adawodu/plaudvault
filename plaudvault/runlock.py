"""A single-writer lock for pipeline runs.

With a scheduled sync four times a day plus a "Sync now" button in the console, two
runs overlapping is a matter of when, not if. Two processes writing the same SQLite
manifest produced real, observed corruption ("database disk image is malformed") —
WAL makes that survivable, this makes it not happen.

Advisory and file-based, so it works the same on macOS and Linux and needs no daemon.
A lock whose PID is gone is treated as stale and taken over, so a killed run doesn't
wedge the pipeline forever.
"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

LOCK_NAME = "run.lock"
STALE_AFTER = 6 * 3600  # a genuinely stuck run, not just a slow transcription

# The lock is deleted when a run ends, so it cannot answer "when did this last run?".
# That question is the difference between a quiet archive and a broken scheduler, so
# the answer is written down separately as a receipt that outlives the run.
RECEIPT_NAME = "last-run.json"


class RunInProgress(RuntimeError):
    pass


def lock_path(archive_root: Path) -> Path:
    return archive_root / LOCK_NAME


def receipt_path(archive_root: Path) -> Path:
    return archive_root / RECEIPT_NAME


def record_run(archive_root: Path, *, started: float, rc: int) -> None:
    """Write the last-run receipt. Best effort — a failure here must not fail a run."""
    payload = {
        "started": started,
        "finished": time.time(),
        "seconds": round(time.time() - started, 1),
        "rc": rc,
    }
    try:
        path = receipt_path(archive_root)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload))
        tmp.replace(path)
    except OSError:
        pass


def last_run(archive_root: Path) -> dict | None:
    try:
        return json.loads(receipt_path(archive_root).read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    return True


def read(archive_root: Path) -> dict | None:
    path = lock_path(archive_root)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _is_stale(info: dict) -> bool:
    if not info:
        return True
    if not _alive(int(info.get("pid", -1))):
        return True
    return (time.time() - float(info.get("started", 0))) > STALE_AFTER


@contextmanager
def acquire(archive_root: Path, *, what: str = "run"):
    """Take the lock for the duration of the block, or raise RunInProgress."""
    path = lock_path(archive_root)
    path.parent.mkdir(parents=True, exist_ok=True)

    existing = read(archive_root)
    if existing and not _is_stale(existing):
        age = int(time.time() - float(existing.get("started", 0)))
        raise RunInProgress(
            f"another {existing.get('what', 'run')} is active "
            f"(pid {existing.get('pid')}, {age // 60}m {age % 60}s ago). "
            "Wait for it to finish, or remove " + str(path) + " if you know it is dead."
        )
    if existing:
        print(f"  [info] clearing stale lock from pid {existing.get('pid')}")

    payload = {"pid": os.getpid(), "started": time.time(), "what": what}
    tmp = path.with_suffix(".lock.tmp")
    tmp.write_text(json.dumps(payload))
    tmp.replace(path)
    try:
        yield
    finally:
        try:
            current = json.loads(path.read_text())
            if current.get("pid") == os.getpid():
                path.unlink()
        except (OSError, json.JSONDecodeError):
            pass
