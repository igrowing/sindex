"""SQLite storage: schema, connection settings and small query helpers."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from .textnorm import BIGRAM_SEP, RUN_END, RUN_START
from .usn import is_local_folder

SCHEMA_VERSION = 1

# Queue stages and their default priorities (lower runs first).
STAGE_DIR = "dir"
STAGE_TEXT = "text"
STAGE_META = "meta"  # phase 2
STAGE_OCR = "ocr"  # phase 3
PRIORITY = {STAGE_DIR: 5, STAGE_TEXT: 10, STAGE_META: 20, STAGE_OCR: 30}
PRIORITY_FORCED = 1
# within STAGE_TEXT: plain text and markup are cheap to parse, so they are read (and become searchable)
# before the slower formats (PDF, Office, RTF); both stay well short of STAGE_META.
PRIORITY_TEXT_EASY = PRIORITY[STAGE_TEXT]
PRIORITY_TEXT_HARD = PRIORITY_TEXT_EASY + 5

# A file's chunks live at rowids [file_id << CHUNK_BITS, (file_id + 1) << CHUNK_BITS).
CHUNK_BITS = 16

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    dir TEXT NOT NULL COLLATE NOCASE,
    name TEXT NOT NULL COLLATE NOCASE,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    status TEXT NOT NULL,        -- ok | truncated | empty | binary | too_big | error
    encoding TEXT,
    error TEXT,
    indexed_at REAL NOT NULL,
    UNIQUE (dir, name)
);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(
    body,
    source UNINDEXED,            -- text | meta | ocr
    tokenize = "unicode61 remove_diacritics 2 separators '{BIGRAM_SEP}{RUN_START}{RUN_END}'"
);

CREATE TABLE IF NOT EXISTS queue (
    path TEXT NOT NULL COLLATE NOCASE,
    stage TEXT NOT NULL,
    priority INTEGER NOT NULL,
    reason TEXT,
    enqueued_at REAL NOT NULL,
    PRIMARY KEY (path, stage)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS queue_order ON queue (priority, enqueued_at);

CREATE TABLE IF NOT EXISTS volumes (
    letter TEXT PRIMARY KEY,
    roots TEXT,                  -- JSON list of roots covered by the last full scan
    fs_type TEXT,
    journal_id INTEGER,
    next_usn INTEGER,
    last_full_scan REAL
);

-- filled from phase 2 (metadata) and phase 3 (OCR)
CREATE TABLE IF NOT EXISTS media (
    file_id INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    kind TEXT, width INTEGER, height INTEGER, duration_s REAL, taken_at TEXT,
    camera TEXT, lat REAL, lon REAL, codec TEXT,
    ocr_hash TEXT, ocr_lang TEXT, ocr_at REAL
);
"""


class DbLocationError(Exception):
    """index.db cannot live where the configuration puts it."""


def check_location(path: Path) -> None:
    """The folder of `path` must exist and be on a local drive: the write-ahead log needs shared memory
    and file locks that network shares do not provide, and a database there gets corrupted."""
    folder = Path(path).parent
    if not folder.is_dir():
        raise DbLocationError(f"the index folder {folder} does not exist (db_dir in config.toml)")
    if not is_local_folder(str(folder)):
        raise DbLocationError(f"the index folder {folder} is not on a local drive; SQLite cannot keep its "
                              "write-ahead log on a network share or a removable drive")


def connect(path: Path, *, readonly: bool = False, wal_pages: int | None = None,
            cache_mb: int = 2) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f"{Path(path).as_uri()}?mode=ro", uri=True, timeout=10,
                               isolation_level=None, check_same_thread=False)
    else:
        conn = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
    conn.execute(f"PRAGMA cache_size = {-1024 * int(cache_mb)}")  # KiB; a write batch must fit in it
    conn.execute("PRAGMA mmap_size = 0")
    conn.execute("PRAGMA temp_store = FILE")
    if not readonly:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")  # WAL: fsync at checkpoints, not on every commit
        conn.execute("PRAGMA foreign_keys = ON")
        if wal_pages is not None:
            conn.execute(f"PRAGMA wal_autocheckpoint = {int(wal_pages)}")
    return conn


def init(conn: sqlite3.Connection) -> None:
    if sqlite3.sqlite_version_info < (3, 35):
        raise RuntimeError(f"SQLite {sqlite3.sqlite_version} is too old (need FTS5, 3.35+)")
    conn.executescript(SCHEMA)
    conn.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))


def get_meta(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def set_meta(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, json.dumps(value)))


def enqueue(conn: sqlite3.Connection, path: str, stage: str = STAGE_TEXT,
            reason: str = "", priority: int | None = None) -> None:
    prio = PRIORITY[stage] if priority is None else priority
    conn.execute(
        """INSERT INTO queue VALUES (?, ?, ?, ?, ?)
           ON CONFLICT (path, stage) DO UPDATE SET
             priority = min(priority, excluded.priority),
             reason = CASE WHEN excluded.priority < priority THEN excluded.reason ELSE reason END""",
        (path, stage, prio, reason, time.time()),
    )


def chunk_range(file_id: int) -> tuple[int, int]:
    return file_id << CHUNK_BITS, ((file_id + 1) << CHUNK_BITS) - 1


def delete_chunks(conn: sqlite3.Connection, file_id: int, source: str | None = None) -> None:
    lo, hi = chunk_range(file_id)
    if source is None:
        conn.execute("DELETE FROM chunks WHERE rowid BETWEEN ? AND ?", (lo, hi))
    else:
        conn.execute("DELETE FROM chunks WHERE rowid BETWEEN ? AND ? AND source = ?", (lo, hi, source))


def delete_file(conn: sqlite3.Connection, file_id: int) -> None:
    delete_chunks(conn, file_id)
    conn.execute("DELETE FROM files WHERE id = ?", (file_id,))


def like_under(root: str) -> str:
    """LIKE pattern (ESCAPE '^') matching directories strictly below `root`."""
    escaped = root.replace("^", "^^").replace("%", "^%").replace("_", "^_")
    return escaped + ("%" if escaped.endswith("\\") else "\\%")
