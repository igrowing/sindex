"""db.upsert_media(): writing structured image/video fields to the `media` table."""

import time

from sindex import db
from sindex.extract import MediaFields


def _make_file(conn, dir_: str, name: str) -> int:
    conn.execute(
        "INSERT INTO files (dir, name, size, mtime_ns, status, indexed_at) VALUES (?, ?, 0, 0, 'ok', ?)",
        (dir_, name, time.time()))
    return conn.execute("SELECT id FROM files WHERE dir = ? AND name = ?", (dir_, name)).fetchone()[0]


def test_upsert_media_inserts_a_row(cfg, tmp_path):
    conn = db.connect(cfg.db_path)
    db.init(conn)
    file_id = _make_file(conn, str(tmp_path), "photo.jpg")

    db.upsert_media(conn, file_id, MediaFields(kind="image", width=12, height=9, camera="Canon"))

    row = conn.execute("SELECT kind, width, height, camera FROM media WHERE file_id = ?", (file_id,)).fetchone()
    assert tuple(row) == ("image", 12, 9, "Canon")
    conn.close()


def test_upsert_media_updates_in_place(cfg, tmp_path):
    conn = db.connect(cfg.db_path)
    db.init(conn)
    file_id = _make_file(conn, str(tmp_path), "photo.jpg")

    db.upsert_media(conn, file_id, MediaFields(kind="image", width=12, height=9))
    db.upsert_media(conn, file_id, MediaFields(kind="image", width=20, height=15, camera="Nikon"))

    rows = conn.execute("SELECT width, height, camera FROM media WHERE file_id = ?", (file_id,)).fetchall()
    assert len(rows) == 1
    assert tuple(rows[0]) == (20, 15, "Nikon")
    conn.close()


def test_delete_file_cascades_to_media(cfg, tmp_path):
    conn = db.connect(cfg.db_path)
    db.init(conn)
    file_id = _make_file(conn, str(tmp_path), "photo.jpg")
    db.upsert_media(conn, file_id, MediaFields(kind="image", width=12, height=9))

    db.delete_file(conn, file_id)

    assert conn.execute("SELECT count(*) FROM media WHERE file_id = ?", (file_id,)).fetchone()[0] == 0
    conn.close()
