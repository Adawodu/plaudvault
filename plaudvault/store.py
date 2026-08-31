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

-- Transcript windows and their embeddings, for semantic search. The vector is raw
-- float32 bytes; `dim` and `model` are stored so a change of embedding model is
-- detectable rather than silently comparing incompatible vectors. Wholly derived from
-- the transcripts — safe to delete and rebuild at any time.
CREATE TABLE IF NOT EXISTS chunks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    recording_id TEXT NOT NULL REFERENCES recordings(id),
    idx          INTEGER NOT NULL,
    start_ms     INTEGER,
    text         TEXT NOT NULL,
    vector       BLOB NOT NULL,
    dim          INTEGER NOT NULL,
    model        TEXT NOT NULL,
    created_at   INTEGER NOT NULL,
    UNIQUE(recording_id, idx)
);
CREATE INDEX IF NOT EXISTS idx_chunks_rec   ON chunks(recording_id);
CREATE INDEX IF NOT EXISTS idx_chunks_model ON chunks(model);

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

-- A person whose voice this archive knows. `voiceprint` is the mean speaker
-- embedding over every turn ever confirmed as theirs, so naming somebody once
-- carries forward: the next recording matches by voice rather than asking again.
-- `is_me` marks the archive owner — every recording is theirs, so that one is
-- worth knowing for free. `external_ref` is where a CRM/contact id goes; it is
-- deliberately opaque text so plaudvault never has to know whose CRM it is.
CREATE TABLE IF NOT EXISTS speakers (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE,
    is_me         INTEGER NOT NULL DEFAULT 0,
    external_ref  TEXT,               -- 'clarify:rec_123', 'contacts:ABCD', ...
    note          TEXT,
    voiceprint    BLOB,               -- float32 mean embedding, or NULL
    voiceprint_dim INTEGER,
    voiceprint_n  INTEGER NOT NULL DEFAULT 0,  -- turns the mean was built from
    voiceprint_model TEXT,
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER
);

-- One row per anonymous diarization label per recording. The label ('SPEAKER_00')
-- is what the diarizer produced and never changes; `speaker_id` is the human or
-- voiceprint judgement laid over it, and can be corrected at any time without
-- re-running diarization. `source` records WHO decided, so a machine guess is
-- never mistaken for a confirmation.
CREATE TABLE IF NOT EXISTS recording_speakers (
    recording_id TEXT NOT NULL REFERENCES recordings(id),
    label        TEXT NOT NULL,       -- 'SPEAKER_00'
    speaker_id   INTEGER REFERENCES speakers(id),
    source       TEXT NOT NULL DEFAULT 'unassigned',  -- unassigned|voiceprint|human
    confidence   REAL,                -- cosine to the matched voiceprint
    seconds      REAL NOT NULL DEFAULT 0,   -- how long this label spoke
    turns        INTEGER NOT NULL DEFAULT 0,
    embedding    BLOB,                -- this label's mean embedding in THIS recording
    embedding_dim INTEGER,
    decided_at   INTEGER,
    PRIMARY KEY (recording_id, label)
);
CREATE INDEX IF NOT EXISTS idx_recspk_speaker ON recording_speakers(speaker_id);

-- An accepted action handed to an agent to execute. Separate from `actions`
-- because the assignment has its own lifecycle: an agent claims it, works, and
-- reports back, and the report is a PROPOSAL the human still closes. Nothing is
-- ever dispatched that a human did not accept first.
CREATE TABLE IF NOT EXISTS dispatch (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id    INTEGER NOT NULL REFERENCES actions(id),
    agent        TEXT NOT NULL,       -- 'openclaw', 'claude', free text
    status       TEXT NOT NULL DEFAULT 'queued',
                 -- queued | claimed | done | failed | cancelled
    instructions TEXT,                -- what the human wants done, beyond action.text
    result       TEXT,                -- what the agent reported
    error        TEXT,
    claimed_by   TEXT,                -- session/instance that claimed it
    created_at   INTEGER NOT NULL,
    claimed_at   INTEGER,
    finished_at  INTEGER,
    reviewed_at  INTEGER              -- human saw the result
);
CREATE INDEX IF NOT EXISTS idx_dispatch_agent  ON dispatch(agent, status);
CREATE INDEX IF NOT EXISTS idx_dispatch_action ON dispatch(action_id);
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
    ("recordings", "indexed_at", "ALTER TABLE recordings ADD COLUMN indexed_at INTEGER"),
    # A title the archive chose (or you did), as opposed to the date-stamped
    # filename Plaud's device produced. `title_source` keeps a hand-written title
    # from ever being overwritten by a re-run.
    ("recordings", "title", "ALTER TABLE recordings ADD COLUMN title TEXT"),
    ("recordings", "title_source", "ALTER TABLE recordings ADD COLUMN title_source TEXT"),
    ("recordings", "titled_at", "ALTER TABLE recordings ADD COLUMN titled_at INTEGER"),
    ("recordings", "diarized_at", "ALTER TABLE recordings ADD COLUMN diarized_at INTEGER"),
    ("recordings", "diarize_model", "ALTER TABLE recordings ADD COLUMN diarize_model TEXT"),
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

    # A recording tiered `exclude` is noise you have dismissed: it stays on disk with
    # its audio and verification facts, but leaves the console and stops consuming the
    # pipeline. Spelled once, used everywhere, so the two can never disagree.
    NOT_EXCLUDED = (
        "r.id NOT IN (SELECT recording_id FROM triage WHERE tier = 'exclude')"
    )

    def get(self, rec_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM recordings WHERE id = ?", (rec_id,)).fetchone()

    def all(self) -> list[sqlite3.Row]:
        """Every recording, dismissed ones included. Verification and repair read this."""
        return self.db.execute("SELECT * FROM recordings ORDER BY started_at DESC").fetchall()

    def visible(self) -> list[sqlite3.Row]:
        """What the console shows: everything you have not dismissed."""
        return self.db.execute(
            f"SELECT r.* FROM recordings r WHERE {self.NOT_EXCLUDED} "
            "ORDER BY r.started_at DESC"
        ).fetchall()

    def hidden(self) -> list[sqlite3.Row]:
        """The dismissed ones, so they can be reviewed and restored."""
        return self.db.execute(
            "SELECT r.* FROM recordings r JOIN triage t ON t.recording_id = r.id "
            "WHERE t.tier = 'exclude' ORDER BY r.started_at DESC"
        ).fetchall()

    def needing(self, column: str, *, requires: str) -> list[sqlite3.Row]:
        """Rows where `column` is unset but prerequisite `requires` is set."""
        return self.db.execute(
            f"SELECT r.* FROM recordings r WHERE r.{column} IS NULL "
            f"AND r.{requires} IS NOT NULL AND {self.NOT_EXCLUDED} "
            "ORDER BY r.started_at DESC"
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
            f"SELECT r.* FROM recordings r WHERE r.sentiment_at IS NULL "
            f"AND r.transcript_path IS NOT NULL AND {self.NOT_EXCLUDED} "
            "ORDER BY r.started_at DESC"
        ).fetchall()

    # ------------------------------------------------------------------ chunks

    def set_chunks(self, rec_id: str, chunks: list[dict], vectors, *, model: str) -> None:
        """Replace this recording's index. Whole-recording swap, in one transaction —
        a half-reindexed recording would return hits pointing at the wrong moments."""
        dim = int(vectors.shape[1])
        now = int(time.time())
        with self.db:
            self.db.execute("DELETE FROM chunks WHERE recording_id = ?", (rec_id,))
            self.db.executemany(
                "INSERT INTO chunks (recording_id, idx, start_ms, text, vector, dim, model,"
                " created_at) VALUES (?,?,?,?,?,?,?,?)",
                [
                    (rec_id, i, c["start_ms"], c["text"],
                     vectors[i].astype("float32").tobytes(), dim, model, now)
                    for i, c in enumerate(chunks)
                ],
            )

    def chunks(self, *, model: str, include_excluded: bool = False) -> list[sqlite3.Row]:
        q = (
            "SELECT c.recording_id, c.start_ms, c.text, c.vector, r.filename, r.started_at,"
            " t.tier FROM chunks c JOIN recordings r ON r.id = c.recording_id "
            "LEFT JOIN triage t ON t.recording_id = c.recording_id WHERE c.model = ? "
        )
        if not include_excluded:
            q += "AND (t.tier IS NULL OR t.tier != 'exclude') "
        return self.db.execute(q + "ORDER BY c.recording_id, c.idx", (model,)).fetchall()

    def needing_index(self, model: str) -> list[sqlite3.Row]:
        """Transcribed recordings with no chunks for this model — or none at all.

        Keyed on the model, so switching embedding models re-indexes rather than
        leaving a corpus half in one vector space and half in another.
        """
        return self.db.execute(
            f"SELECT r.* FROM recordings r WHERE r.transcript_path IS NOT NULL "
            f"AND {self.NOT_EXCLUDED} "
            "AND NOT EXISTS (SELECT 1 FROM chunks c WHERE c.recording_id = r.id "
            "AND c.model = ?) ORDER BY r.started_at DESC",
            (model,),
        ).fetchall()

    def index_stats(self, model: str) -> dict:
        row = self.db.execute(
            "SELECT COUNT(*) n, COUNT(DISTINCT recording_id) recs FROM chunks WHERE model = ?",
            (model,),
        ).fetchone()
        return {"chunks": row["n"], "recordings": row["recs"]}

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

    # ------------------------------------------------------------------ titles

    def needing_title(self) -> list[sqlite3.Row]:
        """Transcribed and never looked at by the titler.

        Keyed on `titled_at`, not on `title`, for the same reason `sentiment_at`
        exists: a recording the model *looked at* and could not name must not come
        back every run, or the pipeline pays for a call that can never succeed. The
        console clears `titled_at` along with the title, so asking for another
        attempt is still one click.

        Requires a transcript rather than a summary: recordings under
        `summarize_min_seconds` never get summarized, and a two-minute voice memo is
        exactly the thing whose date-stamped filename tells you nothing.
        """
        return self.db.execute(
            f"SELECT r.* FROM recordings r WHERE r.transcript_path IS NOT NULL "
            f"AND r.titled_at IS NULL AND {self.NOT_EXCLUDED} "
            "ORDER BY r.started_at DESC"
        ).fetchall()

    def mark_title_attempted(self, rec_id: str) -> None:
        """The titler looked and declined. Records the visit, leaves the title unset."""
        self.db.execute(
            "UPDATE recordings SET titled_at = ? WHERE id = ?", (int(time.time()), rec_id)
        )
        self.db.commit()

    def set_title(self, rec_id: str, title: str, *, source: str) -> None:
        if source not in ("model", "human"):
            raise ValueError(f"unknown title source {source!r}")
        self.db.execute(
            "UPDATE recordings SET title = ?, title_source = ?, titled_at = ? WHERE id = ?",
            (title.strip()[:200], source, int(time.time()), rec_id),
        )
        self.db.commit()

    # ------------------------------------------------------------------ speakers

    def add_speaker(self, name: str, *, is_me: bool = False, external_ref: str = "",
                    note: str = "") -> int:
        now = int(time.time())
        self.db.execute(
            "INSERT INTO speakers (name, is_me, external_ref, note, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET "
            "is_me = excluded.is_me, "
            "external_ref = COALESCE(NULLIF(excluded.external_ref, ''), speakers.external_ref), "
            "note = COALESCE(NULLIF(excluded.note, ''), speakers.note), "
            "updated_at = excluded.updated_at",
            (name.strip(), int(is_me), external_ref.strip(), note.strip(), now, now),
        )
        self.db.commit()
        return self.db.execute(
            "SELECT id FROM speakers WHERE name = ?", (name.strip(),)
        ).fetchone()[0]

    def speakers(self) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT s.*, "
            "(SELECT COUNT(*) FROM recording_speakers rs WHERE rs.speaker_id = s.id) AS recordings "
            "FROM speakers s ORDER BY s.is_me DESC, s.name"
        ).fetchall()

    def speaker(self, speaker_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM speakers WHERE id = ?", (speaker_id,)).fetchone()

    def me(self) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM speakers WHERE is_me = 1 LIMIT 1").fetchone()

    def update_speaker(self, speaker_id: int, **fields) -> None:
        if not fields:
            return
        fields["updated_at"] = int(time.time())
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(
            f"UPDATE speakers SET {cols} WHERE id = ?", (*fields.values(), speaker_id)
        )
        self.db.commit()

    def delete_speaker(self, speaker_id: int) -> None:
        """Forget a person. Their label assignments revert to unassigned rather than
        vanishing, so the diarization stays intact and can be re-attributed."""
        with self.db:
            self.db.execute(
                "UPDATE recording_speakers SET speaker_id = NULL, source = 'unassigned', "
                "confidence = NULL WHERE speaker_id = ?",
                (speaker_id,),
            )
            self.db.execute("DELETE FROM speakers WHERE id = ?", (speaker_id,))

    def set_recording_speakers(self, rec_id: str, labels: list[dict]) -> None:
        """Write this recording's diarization labels, preserving human decisions.

        Re-running diarization must never silently un-name somebody you named. The
        upsert leaves `speaker_id`/`source` alone when the existing row was decided
        by a human; machine attributions are free to be replaced.
        """
        now = int(time.time())
        with self.db:
            for lb in labels:
                self.db.execute(
                    """
                    INSERT INTO recording_speakers
                        (recording_id, label, seconds, turns, embedding, embedding_dim,
                         source, decided_at)
                    VALUES (?,?,?,?,?,?, 'unassigned', ?)
                    ON CONFLICT(recording_id, label) DO UPDATE SET
                        seconds = excluded.seconds,
                        turns = excluded.turns,
                        embedding = excluded.embedding,
                        embedding_dim = excluded.embedding_dim
                    """,
                    (rec_id, lb["label"], lb.get("seconds", 0.0), lb.get("turns", 0),
                     lb.get("embedding"), lb.get("embedding_dim"), now),
                )

    def attribute(self, rec_id: str, label: str, speaker_id: int | None, *,
                  source: str, confidence: float | None = None) -> None:
        if source not in ("unassigned", "voiceprint", "human"):
            raise ValueError(f"unknown attribution source {source!r}")
        self.db.execute(
            "UPDATE recording_speakers SET speaker_id = ?, source = ?, confidence = ?, "
            "decided_at = ? WHERE recording_id = ? AND label = ?",
            (speaker_id, source, confidence, int(time.time()), rec_id, label),
        )
        self.db.commit()

    def recording_speakers(self, rec_id: str) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT rs.*, s.name, s.is_me, s.external_ref FROM recording_speakers rs "
            "LEFT JOIN speakers s ON s.id = rs.speaker_id "
            "WHERE rs.recording_id = ? ORDER BY rs.seconds DESC",
            (rec_id,),
        ).fetchall()

    def confirmed_embeddings(self, speaker_id: int) -> list[sqlite3.Row]:
        """Every per-recording embedding a human confirmed as this person.

        Only `human` rows count. Building a voiceprint out of the machine's own
        guesses would let one bad match compound into a drifting identity.
        """
        return self.db.execute(
            "SELECT embedding, embedding_dim, seconds FROM recording_speakers "
            "WHERE speaker_id = ? AND source = 'human' AND embedding IS NOT NULL",
            (speaker_id,),
        ).fetchall()

    def needing_diarization(self) -> list[sqlite3.Row]:
        return self.db.execute(
            f"SELECT r.* FROM recordings r WHERE r.diarized_at IS NULL "
            f"AND r.transcript_path IS NOT NULL AND r.audio_path IS NOT NULL "
            f"AND {self.NOT_EXCLUDED} ORDER BY r.started_at DESC"
        ).fetchall()

    def unnamed_labels(self) -> list[sqlite3.Row]:
        """Diarized voices nobody has put a name to — the speaker inbox."""
        return self.db.execute(
            "SELECT rs.*, r.filename, r.title, r.started_at FROM recording_speakers rs "
            "JOIN recordings r ON r.id = rs.recording_id "
            "WHERE rs.speaker_id IS NULL ORDER BY rs.seconds DESC"
        ).fetchall()

    # ------------------------------------------------------------------ dispatch

    def add_dispatch(self, action_id: int, agent: str, *, instructions: str = "") -> int:
        cur = self.db.execute(
            "INSERT INTO dispatch (action_id, agent, instructions, created_at) VALUES (?,?,?,?)",
            (action_id, agent.strip().lower(), instructions.strip()[:2000], int(time.time())),
        )
        self.db.commit()
        return cur.lastrowid

    def dispatches(self, *, agent: str | None = None, status: str | None = None,
                   action_id: int | None = None) -> list[sqlite3.Row]:
        q = ("SELECT d.*, a.text AS action_text, a.owner, a.intent, a.due_at, a.quote, "
             "a.recording_id FROM dispatch d JOIN actions a ON a.id = d.action_id WHERE 1=1")
        args: list = []
        if agent:
            q += " AND d.agent = ?"
            args.append(agent.strip().lower())
        if status:
            q += " AND d.status = ?"
            args.append(status)
        if action_id:
            q += " AND d.action_id = ?"
            args.append(action_id)
        return self.db.execute(q + " ORDER BY d.created_at DESC", args).fetchall()

    def get_dispatch(self, dispatch_id: int) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT d.*, a.text AS action_text, a.owner, a.intent, a.due_at, a.quote, "
            "a.recording_id FROM dispatch d JOIN actions a ON a.id = d.action_id WHERE d.id = ?",
            (dispatch_id,),
        ).fetchone()

    def claim_dispatch(self, dispatch_id: int, claimed_by: str) -> bool:
        """Take a queued job. Returns False if somebody already has it.

        The status guard is in the UPDATE, not a read-then-write, so two agents
        polling the same queue cannot both believe they won.
        """
        cur = self.db.execute(
            "UPDATE dispatch SET status = 'claimed', claimed_by = ?, claimed_at = ? "
            "WHERE id = ? AND status = 'queued'",
            (claimed_by[:200], int(time.time()), dispatch_id),
        )
        self.db.commit()
        return cur.rowcount == 1

    def finish_dispatch(self, dispatch_id: int, *, ok: bool, result: str = "",
                        error: str = "") -> bool:
        cur = self.db.execute(
            "UPDATE dispatch SET status = ?, result = ?, error = ?, finished_at = ? "
            "WHERE id = ? AND status = 'claimed'",
            ("done" if ok else "failed", result[:8000], error[:2000],
             int(time.time()), dispatch_id),
        )
        self.db.commit()
        return cur.rowcount == 1

    def update_dispatch(self, dispatch_id: int, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(
            f"UPDATE dispatch SET {cols} WHERE id = ?", (*fields.values(), dispatch_id)
        )
        self.db.commit()

    def update(self, rec_id: str, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(
            f"UPDATE recordings SET {cols} WHERE id = ?", (*fields.values(), rec_id)
        )
        self.db.commit()
