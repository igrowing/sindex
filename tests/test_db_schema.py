"""Schema 2: folders in `dirs`, listed files in `names`, and the conversion of a schema 1 index."""

import sqlite3

import pytest

from sindex import db
from sindex.extract import MediaFields

# schema 1, as sindex 0.2.8 created it (only the tables the conversion touches differ from schema 2)
SCHEMA_V1_FILES = """
CREATE TABLE files (
    id INTEGER PRIMARY KEY,
    dir TEXT NOT NULL COLLATE NOCASE,
    name TEXT NOT NULL COLLATE NOCASE,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    status TEXT NOT NULL,
    encoding TEXT,
    error TEXT,
    indexed_at REAL NOT NULL,
    UNIQUE (dir, name)
);
"""


def _v1_index(path):
    conn = db.connect(path)
    conn.executescript(SCHEMA_V1_FILES)
    # the rest of schema 1 is schema 2 without dirs, names and the new files table, which exist already here
    conn.executescript(db.SCHEMA.replace(db._DIRS + ";", "").replace(db._NAMES + ";", "")
                       .replace(db._FILES_PLACE + ";", ""))
    conn.execute("INSERT INTO meta VALUES ('schema_version', '1')")
    rows = [(7, "D:\\docs", "a.txt", "ok"), (9, "D:\\docs", "b.doc", "name_only"),
            (12, "d:\\DOCS\\sub", "c.jpg", "ok"), (13, "D:\\docs\\sub", "d.pdf", "error")]
    for fid, folder, name, status in rows:
        conn.execute("INSERT INTO files VALUES (?, ?, ?, 10, 20, ?, NULL, NULL, 30)", (fid, folder, name, status))
    conn.execute("INSERT INTO chunks (rowid, body, source) VALUES (?, 'alpha', 'text')", (7 << db.CHUNK_BITS,))
    db.upsert_media(conn, 12, MediaFields(kind="image", width=640))
    return conn


def test_a_schema_1_index_is_converted_keeping_ids_text_and_media(tmp_path):
    conn = _v1_index(tmp_path / "index.db")

    db.init(conn)

    assert db.get_meta(conn, "schema_version") == 2
    assert conn.execute("SELECT id, path FROM dirs ORDER BY id").fetchall() == [(1, "D:\\docs"), (2, "d:\\DOCS\\sub")]
    assert conn.execute("SELECT id, dir_id, name, status FROM files ORDER BY id").fetchall() == [
        (7, 1, "a.txt", "ok"), (12, 2, "c.jpg", "ok"), (13, 2, "d.pdf", "error")]
    assert conn.execute("SELECT dir_id, name FROM names").fetchall() == [(1, "b.doc")]
    assert conn.execute("SELECT rowid >> ? FROM chunks WHERE chunks MATCH 'alpha'",
                        (db.CHUNK_BITS,)).fetchall() == [(7,)]
    assert conn.execute("SELECT file_id, width FROM media").fetchall() == [(12, 640)]  # not cascaded away
    assert conn.execute("PRAGMA foreign_keys").fetchone() == (1,)
    conn.close()


def test_the_converted_index_enforces_one_row_per_file(tmp_path):
    conn = _v1_index(tmp_path / "index.db")
    db.init(conn)

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO files VALUES (99, 1, 'A.TXT', 1, 1, 'ok', NULL, NULL, 1)")
    conn.close()


def test_converting_twice_changes_nothing(tmp_path):
    conn = _v1_index(tmp_path / "index.db")
    db.init(conn)
    before = conn.execute("SELECT * FROM files ORDER BY id").fetchall()

    db.init(conn)

    assert conn.execute("SELECT * FROM files ORDER BY id").fetchall() == before
    conn.close()


def test_a_folder_gets_one_number_and_keeps_it(tmp_path):
    conn = db.connect(tmp_path / "index.db")
    db.init(conn)

    first = db.add_dir(conn, "D:\\docs")

    assert db.add_dir(conn, "d:\\DOCS") == first  # folders compare without case, as Windows does
    assert db.find_dir(conn, "D:\\docs") == first
    assert db.find_dir(conn, "D:\\other") is None
    conn.close()


def test_a_folder_is_forgotten_only_when_nothing_is_left_in_it(tmp_path):
    conn = db.connect(tmp_path / "index.db")
    db.init(conn)
    kept, listed = db.add_dir(conn, "D:\\a"), db.add_dir(conn, "D:\\a\\b")
    db.add_dir(conn, "D:\\a\\c")  # nothing in it
    conn.execute("INSERT INTO files (dir_id, name, size, mtime_ns, status, indexed_at) VALUES (?, 'x.txt', 1, 1, "
                 "'ok', 1)", (kept,))
    db.list_name(conn, listed, "y.doc")

    assert db.drop_empty_dirs(conn, "D:\\a") == 1
    assert conn.execute("SELECT id FROM dirs ORDER BY id").fetchall() == [(kept,), (listed,)]
    db.unlist_name(conn, listed, "y.doc")
    db.drop_dir_if_empty(conn, listed)
    db.drop_dir_if_empty(conn, kept)
    assert conn.execute("SELECT id FROM dirs").fetchall() == [(kept,)]
    conn.close()
