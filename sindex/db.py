"""SQLite storage: schema, connection settings and small query helpers."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path

from .extract import MediaFields
from .textnorm import BIGRAM_SEP, RUN_END, RUN_START
from .usn import is_local_folder

log = logging.getLogger(__name__)

SCHEMA_VERSION = 3  # 2: folders in their own table, files listed by name in `names`; 3: files.text_bytes
INDEX_NAME = "index.db"
_INDEX_TABLES = frozenset({"meta", "files", "chunks", "queue"})  # what a file needs to be taken for an index

NAME_ONLY = "name_only"  # the status shown for a file of the `names` table: listed by its name alone

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
# images/videos (STAGE_META): after easy text, before PDF/Office/RTF. PRIORITY[STAGE_META] (20) stays the
# stage's nominal priority; media files use this instead, so they sit strictly between the two text tiers.
PRIORITY_MEDIA = PRIORITY_TEXT_EASY + 2

# A file's chunks live at rowids [file_id << CHUNK_BITS, (file_id + 1) << CHUNK_BITS).
CHUNK_BITS = 16

# A folder path is stored once, in `dirs`; files and names refer to it by number. With the path in every file row,
# and again in the index on (path, name), the paths were most of the size of those tables. `names` holds the files
# whose content is not read (index_all_names), with only what a search in names needs: no row id, size or date.
_FILES_COLUMNS = """
    id INTEGER PRIMARY KEY,
    dir_id INTEGER NOT NULL,     -- dirs.id
    name TEXT NOT NULL COLLATE NOCASE,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    status TEXT NOT NULL,        -- ok | truncated | empty | binary | too_big | error | indexing
    encoding TEXT,
    error TEXT,
    indexed_at REAL NOT NULL,
    text_bytes INTEGER           -- stored text (UTF-8 bytes); NULL while indexing and until measured (schema < 3)
"""
_FILES_PLACE = "CREATE UNIQUE INDEX IF NOT EXISTS files_place ON files (dir_id, name)"
_DIRS = """CREATE TABLE IF NOT EXISTS dirs (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL COLLATE NOCASE UNIQUE
)"""
_NAMES = """CREATE TABLE IF NOT EXISTS names (
    dir_id INTEGER NOT NULL,     -- dirs.id
    name TEXT NOT NULL COLLATE NOCASE,
    PRIMARY KEY (dir_id, name)
) WITHOUT ROWID"""

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

{_DIRS};

CREATE TABLE IF NOT EXISTS files ({_FILES_COLUMNS});
{_FILES_PLACE};

{_NAMES};

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


def check_location(path: Path, what: str = "index folder") -> None:
    """The folder of `path` must exist and be on a local drive: the write-ahead log needs shared memory
    and file locks that network shares do not provide, and a database there gets corrupted."""
    folder = Path(path).parent
    if not folder.is_dir():
        raise DbLocationError(f"the {what} {folder} does not exist")
    if not is_local_folder(str(folder)):
        raise DbLocationError(f"the {what} {folder} is not on a local drive; SQLite cannot keep its "
                              "write-ahead log on a network share or a removable drive")


def check_index_file(path: Path) -> None:
    """`path` must be an index of this program (or an older one): a pre-built index is taken on trust otherwise, and
    a wrong file would only show up later as an indexer that fails on every start."""
    try:
        conn = connect(path, readonly=True)
        try:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            version = get_meta(conn, "schema_version") if "meta" in tables else None
        finally:
            conn.close()
    except sqlite3.DatabaseError as e:  # not a database at all, damaged, or locked for good
        raise DbLocationError(f"{path} cannot be read as an index: {e}") from e
    if not _INDEX_TABLES <= tables or version is None:
        raise DbLocationError(f"{path} is a database, but not an index of sindex")
    if not isinstance(version, int) or version > SCHEMA_VERSION:
        raise DbLocationError(f"{path} was made by a newer sindex (schema {version}, this one reads {SCHEMA_VERSION})")


def checkpoint(path: Path) -> None:
    """Merge the write-ahead log into `path`, so that the one file holds the whole index (for copying it).
    The indexer must not be running: DbLocationError if the log cannot be emptied."""
    conn = connect(path)
    try:
        busy, _, _ = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    finally:
        conn.close()
    if busy:
        raise DbLocationError(f"{path} is in use by another program; try again when nothing reads it")


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
    has_meta = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'").fetchone()
    version = get_meta(conn, "schema_version") if has_meta else None
    if version == 1:
        _migrate_to_v2(conn)
    if version in (1, 2):
        _migrate_to_v3(conn)
    conn.executescript(SCHEMA)
    conn.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))


def _migrate_to_v2(conn: sqlite3.Connection) -> None:
    """Schema 1 -> 2: the folder of every file moves to `dirs`, files listed by name alone move to `names`.

    The files table is rebuilt (SQLite cannot drop a column that is part of a UNIQUE constraint), keeping the file
    ids, which the chunks' rowids are made of. Folders are numbered in path order and the index on (folder, name)
    is built after the rows are in, so that both come out densely packed. One transaction: a crash leaves schema 1.
    The pages of the old table become free space that later writes reuse; the file does not shrink without a VACUUM.
    """
    started = time.monotonic()
    # with foreign keys on, dropping `files` would delete every row of `media` (ON DELETE CASCADE)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("BEGIN")
    try:
        conn.execute(_DIRS)
        conn.execute(_NAMES)
        conn.execute("INSERT INTO dirs (path) SELECT DISTINCT dir FROM files ORDER BY dir")
        conn.execute(f"CREATE TABLE files_v2 ({_FILES_COLUMNS})")
        conn.execute(
            """INSERT INTO files_v2 (id, dir_id, name, size, mtime_ns, status, encoding, error, indexed_at)
               SELECT f.id, d.id, f.name, f.size, f.mtime_ns, f.status, f.encoding, f.error, f.indexed_at
               FROM files f JOIN dirs d ON d.path = f.dir WHERE f.status != ? ORDER BY f.id""", (NAME_ONLY,))
        conn.execute("""INSERT INTO names (dir_id, name)
                        SELECT d.id, f.name FROM files f JOIN dirs d ON d.path = f.dir WHERE f.status = ?
                        ORDER BY d.id, f.name""", (NAME_ONLY,))
        conn.execute("DROP TABLE files")
        conn.execute("ALTER TABLE files_v2 RENAME TO files")
        conn.execute(_FILES_PLACE)
        conn.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (json.dumps(2),))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
    log.info("index converted to schema 2 in %.1f s", time.monotonic() - started)


def _migrate_to_v3(conn: sqlite3.Connection) -> None:
    """Schema 2 -> 3: files.text_bytes, the size of a file's stored text, for the disk use report. Adding a column
    rewrites nothing; the indexer measures the files that are already there while it is idle."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(files)")}
    conn.execute("BEGIN")
    try:
        if "text_bytes" not in columns:  # a schema 1 index got the column with its new files table
            conn.execute("ALTER TABLE files ADD COLUMN text_bytes INTEGER")
        conn.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (json.dumps(3),))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


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


def upsert_media(conn: sqlite3.Connection, file_id: int, fields: MediaFields) -> None:
    conn.execute(
        """INSERT INTO media (file_id, kind, width, height, duration_s, taken_at, camera, lat, lon, codec)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (file_id) DO UPDATE SET
             kind = excluded.kind, width = excluded.width, height = excluded.height,
             duration_s = excluded.duration_s, taken_at = excluded.taken_at, camera = excluded.camera,
             lat = excluded.lat, lon = excluded.lon, codec = excluded.codec""",
        (file_id, fields.kind, fields.width, fields.height, fields.duration_s, fields.taken_at,
         fields.camera, fields.lat, fields.lon, fields.codec))


def find_dir(conn: sqlite3.Connection, path: str) -> int | None:
    """The number of a folder; None when nothing of it is in the index."""
    row = conn.execute("SELECT id FROM dirs WHERE path = ?", (path,)).fetchone()
    return row[0] if row else None


def add_dir(conn: sqlite3.Connection, path: str) -> int:
    """The number of a folder, given one if it has none yet."""
    found = find_dir(conn, path)
    if found is not None:
        return found
    return conn.execute("INSERT INTO dirs (path) VALUES (?) RETURNING id", (path,)).fetchone()[0]


def drop_dir_if_empty(conn: sqlite3.Connection, dir_id: int) -> None:
    conn.execute("""DELETE FROM dirs WHERE id = ? AND NOT EXISTS (SELECT 1 FROM files WHERE dir_id = ?)
                    AND NOT EXISTS (SELECT 1 FROM names WHERE dir_id = ?)""", (dir_id, dir_id, dir_id))


def drop_empty_dirs(conn: sqlite3.Connection, top: str) -> int:
    """Forget the folders at and below `top` that no longer hold an indexed or listed file."""
    return conn.execute(
        """DELETE FROM dirs WHERE (path = ? OR path LIKE ? ESCAPE '^')
           AND NOT EXISTS (SELECT 1 FROM files WHERE dir_id = dirs.id)
           AND NOT EXISTS (SELECT 1 FROM names WHERE dir_id = dirs.id)""", (top, like_under(top))).rowcount


def list_name(conn: sqlite3.Connection, dir_id: int, name: str) -> None:
    """Keep a file whose content is not read in the index by its name alone."""
    conn.execute("INSERT OR IGNORE INTO names (dir_id, name) VALUES (?, ?)", (dir_id, name))


def unlist_name(conn: sqlite3.Connection, dir_id: int, name: str) -> None:
    conn.execute("DELETE FROM names WHERE dir_id = ? AND name = ?", (dir_id, name))


def stored_text_bytes(conn: sqlite3.Connection, file_id: int) -> int:
    """UTF-8 bytes of the text stored for a file (0 when it has none)."""
    lo, hi = chunk_range(file_id)
    return conn.execute("SELECT coalesce(sum(length(CAST(body AS BLOB))), 0) FROM chunks WHERE rowid BETWEEN ? AND ?",
                        (lo, hi)).fetchone()[0]


def delete_file(conn: sqlite3.Connection, file_id: int) -> None:
    delete_chunks(conn, file_id)
    conn.execute("DELETE FROM files WHERE id = ?", (file_id,))


def like_under(root: str) -> str:
    """LIKE pattern (ESCAPE '^') matching directories strictly below `root`."""
    escaped = root.replace("^", "^^").replace("%", "^%").replace("_", "^_")
    return escaped + ("%" if escaped.endswith("\\") else "\\%")
