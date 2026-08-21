"""SQLite manifest — the source of truth for what is safely archived.

Every destructive decision (pruning from Plaud's cloud) reads from here, so this
table records verification facts, not intentions: the sha256 we computed after
writing the file, the md5 Plaud claimed, and whether they agreed.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS recordings (
    id                TEXT PRIMARY KEY,
    filename          TEXT NOT NULL,
    started_at        INTEGER NOT NULL,   -- unix seconds
    duration_s        REAL    NOT NULL,
    serial_number     TEXT,
    remote_md5        TEXT,
    remote_size       INTEGER,

    audio_path        TEXT,
    audio_sha256      TEXT,
    audio_size        INTEGER,
    md5_verified      INTEGER DEFAULT 0,
    downloaded_at     INTEGER,

    transcript_path   TEXT,
    transcribed_at    INTEGER,
    transcribe_model  TEXT,

    summary_path      TEXT,
    summarized_at     INTEGER,
    summary_model     TEXT,

    note_path         TEXT,
    noted_at          INTEGER,

    pruned_at         INTEGER,
    meta_json         TEXT
);
CREATE INDEX IF NOT EXISTS idx_started ON recordings(started_at);
CREATE INDEX IF NOT EXISTS idx_pruned  ON recordings(pruned_at);

-- Triage decisions. `tier` drives what physically reaches the cognitive stack;
-- `marked_for_prune` is the ONLY thing that authorises cloud deletion.
CREATE TABLE IF NOT EXISTS triage (
    recording_id     TEXT PRIMARY KEY REFERENCES recordings(id),
    tier             TEXT NOT NULL,      -- 'stack' | 'local' | 'exclude'
    marked_for_prune INTEGER DEFAULT 0,
    note             TEXT,
    decided_at       INTEGER NOT NULL
);

-- A commitment extracted from a recording, or added by hand.
CREATE TABLE IF NOT EXISTS actions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    recording_id  TEXT REFERENCES recordings(id),
    text          TEXT NOT NULL,
    owner         TEXT,
    -- What outcome accepting this action is supposed to produce. Stated at accept
    -- time so outcome scoring later has something honest to score against.
    intent        TEXT,
    kind          TEXT DEFAULT 'commitment',  -- commitment | suggestion | manual
    quote         TEXT,               -- the transcript line it came from
    at_ms         INTEGER,            -- timestamp within the recording
    due_at        INTEGER,
    status        TEXT NOT NULL DEFAULT 'proposed',
                  -- proposed | accepted | in_progress | done | dropped
    system_id     INTEGER REFERENCES systems(id),
    outcome_score INTEGER,            -- 1-5, set at completion
    outcome_note  TEXT,
    created_at    INTEGER NOT NULL,
    accepted_at   INTEGER,
    completed_at  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_actions_status ON actions(status);
CREATE INDEX IF NOT EXISTS idx_actions_rec    ON actions(recording_id);

-- A recurring commitment promoted to a named practice.
CREATE TABLE IF NOT EXISTS systems (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    description TEXT,
    cadence     TEXT,                 -- free text: 'weekly', 'each standup', ...
    created_at  INTEGER NOT NULL,
    retired_at  INTEGER
);

-- One tone reading per recording, produced by the same local model that writes
-- summaries. This is an ESTIMATE over automatic speech recognition with no tone of
-- voice in it, so `confidence` is stored alongside and the console never presents a
-- reading without it. `segments_json` keeps the within-recording arc the aggregate
-- was reduced from, so a flat mean can always be opened up.
CREATE TABLE IF NOT EXISTS sentiment (
    recording_id  TEXT PRIMARY KEY REFERENCES recordings(id),
    valence       REAL NOT NULL,      -- -1 hostile/distressed .. 0 neutral .. 1 warm
    energy        REAL,               -- 0 flat .. 1 heated; independent of valence
    label         TEXT,               -- positive | negative | neutral | mixed
    confidence    REAL,               -- 0..1, the model's own
    spread        REAL,               -- max-min segment valence within the recording
    drivers       TEXT,               -- short phrases, newline separated
    segments_json TEXT,
    model         TEXT,
    scored_at     INTEGER NOT NULL
);

-- Append-only. Cycle time and adherence are computed from this, never from
-- mutable columns, so history survives edits.
CREATE TABLE IF NOT EXISTS action_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id INTEGER NOT NULL REFERENCES actions(id),
    from_status TEXT,
    to_status   TEXT NOT NULL,
    at        INTEGER NOT NULL,
    note      TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_action ON action_events(action_id);
"""

# Applied after SCHEMA, guarded — lets an existing manifest.sqlite gain columns.
MIGRATIONS: list[tuple[str, str, str]] = [
    # (table, column, DDL)
    ("recordings", "extracted_at", "ALTER TABLE recordings ADD COLUMN extracted_at INTEGER"),
    ("actions", "kind", "ALTER TABLE actions ADD COLUMN kind TEXT DEFAULT 'commitment'"),
    ("recordings", "size_ok", "ALTER TABLE recordings ADD COLUMN size_ok INTEGER DEFAULT 0"),
    ("recordings", "audio_kind", "ALTER TABLE recordings ADD COLUMN audio_kind TEXT"),
    # Set whenever sentiment scoring has *looked* at a recording — including when it
    # declined to score one for being too short. Without that distinction, every run
    # would re-scan the same unscorable clips forever.
    ("recordings", "sentiment_at", "ALTER TABLE recordings ADD COLUMN sentiment_at INTEGER"),
]


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        # WAL lets a reader (the console) coexist with a writer (a pipeline run);
        # busy_timeout makes a concurrent writer wait rather than error out. Without
        # both, a scheduled run overlapping a UI-triggered one corrupts reads.
        self.db = sqlite3.connect(path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        for table, column, ddl in MIGRATIONS:
            cols = {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                self.db.execute(ddl)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.db.close()

    # ------------------------------------------------------------------ reads

    def get(self, rec_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM recordings WHERE id = ?", (rec_id,)).fetchone()

    def all(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM recordings ORDER BY started_at DESC").fetchall()

    def needing(self, column: str, *, requires: str) -> list[sqlite3.Row]:
        """Rows where `column` is unset but prerequisite `requires` is set."""
        return self.db.execute(
            f"SELECT * FROM recordings WHERE {column} IS NULL AND {requires} IS NOT NULL "
            "ORDER BY started_at DESC"
        ).fetchall()

    def stale_notes(self) -> list[sqlite3.Row]:
        """Notes the manifest has outgrown — scored after they were written, or gone.

        The missing-file case needs a stat per note, so it is checked in Python
        rather than SQL; the set is small because it only covers written notes.
        """
        outdated = self.db.execute(
            "SELECT r.* FROM recordings r JOIN sentiment s ON s.recording_id = r.id "
            "WHERE r.note_path IS NOT NULL AND s.scored_at > COALESCE(r.noted_at, 0) "
            "ORDER BY r.started_at DESC"
        ).fetchall()
        seen = {r["id"] for r in outdated}
        gone = [
            r
            for r in self.db.execute(
                "SELECT * FROM recordings WHERE note_path IS NOT NULL"
            ).fetchall()
            if r["id"] not in seen and not Path(r["note_path"]).exists()
        ]
        return outdated + gone

    def prunable(self, min_age_days: int, *, require_note: bool = True) -> list[sqlite3.Row]:
        """Fully processed, integrity-verified, older than the floor, not yet pruned."""
        cutoff = int(time.time()) - min_age_days * 86400
        note_clause = "AND note_path IS NOT NULL " if require_note else ""
        return self.db.execute(
            "SELECT * FROM recordings WHERE pruned_at IS NULL AND size_ok = 1 "
            "AND audio_kind IS NOT NULL "
            "AND audio_sha256 IS NOT NULL AND transcript_path IS NOT NULL "
            f"{note_clause}AND started_at < ? ORDER BY started_at ASC",
            (cutoff,),
        ).fetchall()

    def counts(self) -> dict[str, int]:
        q = lambda w: self.db.execute(f"SELECT COUNT(*) FROM recordings WHERE {w}").fetchone()[0]  # noqa: E731
        return {
            "total": self.db.execute("SELECT COUNT(*) FROM recordings").fetchone()[0],
            "downloaded": q("audio_sha256 IS NOT NULL"),
            "verified": q("size_ok = 1 AND audio_kind IS NOT NULL"),
            "md5_exact": q("md5_verified = 1"),
            "transcribed": q("transcript_path IS NOT NULL"),
            "summarized": q("summary_path IS NOT NULL"),
            "scored": self.db.execute("SELECT COUNT(*) FROM sentiment").fetchone()[0],
            "noted": q("note_path IS NOT NULL"),
            "pruned": q("pruned_at IS NOT NULL"),
        }

    # ------------------------------------------------------------------ writes

    def upsert_remote(self, rec) -> None:
        self.db.execute(
            """
            INSERT INTO recordings (id, filename, started_at, duration_s, serial_number,
                                    remote_md5, remote_size, meta_json)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                filename = excluded.filename,
                remote_md5 = COALESCE(excluded.remote_md5, recordings.remote_md5),
                remote_size = excluded.remote_size,
                meta_json = excluded.meta_json
            """,
            (
                rec.id,
                rec.filename,
                int(rec.start_time_ms / 1000),
                rec.duration_s,
                rec.serial_number,
                rec.file_md5 or None,
                rec.filesize,
                json.dumps(rec.raw),
            ),
        )
        self.db.commit()

    # ------------------------------------------------------------------ triage

    def set_triage(self, rec_id: str, tier: str, *, marked_for_prune: bool, note: str = "") -> None:
        if tier not in ("stack", "local", "exclude"):
            raise ValueError(f"unknown tier {tier!r}")
        self.db.execute(
            """
            INSERT INTO triage (recording_id, tier, marked_for_prune, note, decided_at)
            VALUES (?,?,?,?,?)
            ON CONFLICT(recording_id) DO UPDATE SET
                tier = excluded.tier,
                marked_for_prune = excluded.marked_for_prune,
                note = excluded.note,
                decided_at = excluded.decided_at
            """,
            (rec_id, tier, int(marked_for_prune), note, int(time.time())),
        )
        self.db.commit()

    def triage_of(self, rec_id: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM triage WHERE recording_id = ?", (rec_id,)
        ).fetchone()

    def untriaged(self) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT r.* FROM recordings r LEFT JOIN triage t ON t.recording_id = r.id "
            "WHERE t.recording_id IS NULL AND r.transcript_path IS NOT NULL "
            "ORDER BY r.started_at DESC"
        ).fetchall()

    def by_tier(self, tier: str) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT r.* FROM recordings r JOIN triage t ON t.recording_id = r.id "
            "WHERE t.tier = ? ORDER BY r.started_at DESC",
            (tier,),
        ).fetchall()

    # ------------------------------------------------------------------ sentiment

    def set_sentiment(
        self,
        rec_id: str,
        *,
        valence: float,
        energy: float | None,
        label: str,
        confidence: float | None,
        spread: float | None,
        drivers: list[str],
        segments: list[dict],
        model: str,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO sentiment (recording_id, valence, energy, label, confidence,
                                   spread, drivers, segments_json, model, scored_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(recording_id) DO UPDATE SET
                valence = excluded.valence, energy = excluded.energy,
                label = excluded.label, confidence = excluded.confidence,
                spread = excluded.spread, drivers = excluded.drivers,
                segments_json = excluded.segments_json, model = excluded.model,
                scored_at = excluded.scored_at
            """,
            (
                rec_id, valence, energy, label, confidence, spread,
                "\n".join(drivers), json.dumps(segments), model, int(time.time()),
            ),
        )
        self.db.commit()

    def sentiment_of(self, rec_id: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM sentiment WHERE recording_id = ?", (rec_id,)
        ).fetchone()

    def sentiment_series(self, *, since: int | None = None) -> list[sqlite3.Row]:
        """Every scored recording joined to what it was, oldest first.

        Ordered ascending because this feeds a time series; every other read in
        this module is newest-first for list views.
        """
        q = (
            "SELECT s.*, r.filename, r.started_at, r.duration_s, t.tier "
            "FROM sentiment s JOIN recordings r ON r.id = s.recording_id "
            "LEFT JOIN triage t ON t.recording_id = s.recording_id "
        )
        args: list = []
        if since:
            q += "WHERE r.started_at >= ? "
            args.append(since)
        return self.db.execute(q + "ORDER BY r.started_at ASC", args).fetchall()

    def needing_sentiment(self) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM recordings WHERE sentiment_at IS NULL "
            "AND transcript_path IS NOT NULL ORDER BY started_at DESC"
        ).fetchall()

    # ------------------------------------------------------------------ actions

    def add_action(self, **f) -> int:
        f.setdefault("created_at", int(time.time()))
        f.setdefault("status", "proposed")
        cols = ", ".join(f)
        marks = ", ".join("?" * len(f))
        cur = self.db.execute(f"INSERT INTO actions ({cols}) VALUES ({marks})", tuple(f.values()))
        self.db.execute(
            "INSERT INTO action_events (action_id, from_status, to_status, at) VALUES (?,?,?,?)",
            (cur.lastrowid, None, f["status"], f["created_at"]),
        )
        self.db.commit()
        return cur.lastrowid

    def get_action(self, action_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone()

    def actions(self, *, status: str | None = None, recording_id: str | None = None) -> list[sqlite3.Row]:
        q = "SELECT * FROM actions WHERE 1=1"
        args: list = []
        if status:
            q += " AND status = ?"
            args.append(status)
        if recording_id:
            q += " AND recording_id = ?"
            args.append(recording_id)
        q += " ORDER BY COALESCE(due_at, 9e18), created_at DESC"
        return self.db.execute(q, args).fetchall()

    def update_action(self, action_id: int, **fields) -> None:
        """Any status change is journalled to action_events before the row mutates."""
        row = self.get_action(action_id)
        if row is None:
            raise KeyError(action_id)
        now = int(time.time())
        new_status = fields.get("status")
        if new_status and new_status != row["status"]:
            fields.setdefault(
                {"accepted": "accepted_at", "done": "completed_at"}.get(new_status, "_x"), now
            )
            fields.pop("_x", None)
            self.db.execute(
                "INSERT INTO action_events (action_id, from_status, to_status, at, note) "
                "VALUES (?,?,?,?,?)",
                (action_id, row["status"], new_status, now, fields.get("outcome_note")),
            )
        if fields:
            cols = ", ".join(f"{k} = ?" for k in fields)
            self.db.execute(
                f"UPDATE actions SET {cols} WHERE id = ?", (*fields.values(), action_id)
            )
        self.db.commit()

    def action_events(self, action_id: int) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM action_events WHERE action_id = ? ORDER BY at", (action_id,)
        ).fetchall()

    # ------------------------------------------------------------------ systems

    def add_system(self, name: str, description: str = "", cadence: str = "") -> int:
        cur = self.db.execute(
            "INSERT INTO systems (name, description, cadence, created_at) VALUES (?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET description=excluded.description, "
            "cadence=excluded.cadence",
            (name, description, cadence, int(time.time())),
        )
        self.db.commit()
        if cur.lastrowid:
            return cur.lastrowid
        return self.db.execute("SELECT id FROM systems WHERE name = ?", (name,)).fetchone()[0]

    def systems(self) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM systems WHERE retired_at IS NULL ORDER BY name"
        ).fetchall()

    def update(self, rec_id: str, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(
            f"UPDATE recordings SET {cols} WHERE id = ?", (*fields.values(), rec_id)
        )
        self.db.commit()
