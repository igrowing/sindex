"""End to end: indexing images/videos into the `meta` stage, searchable and toggleable."""

from pathlib import Path

from sindex import control, db
from sindex.indexer import Indexer

from .conftest import FIXTURES, make_jpeg
from .test_index_search import names, run_index


def test_photo_metadata_is_indexed_with_source_meta_and_searchable(cfg, tmp_path: Path):
    cfg.index_media_metadata = True
    root = tmp_path / "docs"
    root.mkdir()
    make_jpeg(root / "vacation.jpg", exif={270: "My holiday photo", 271: "Canon", 272: "EOS R5"})
    run_index(cfg, root)

    conn = db.connect(cfg.db_path, readonly=True)
    try:
        file_id = conn.execute("SELECT id FROM files WHERE name = 'vacation.jpg'").fetchone()[0]
        sources = {r[0] for r in conn.execute(
            "SELECT DISTINCT source FROM chunks WHERE rowid BETWEEN ? AND ?",
            db.chunk_range(file_id)).fetchall()}
        assert sources == {"meta"}
        media = conn.execute("SELECT kind, camera FROM media WHERE file_id = ?", (file_id,)).fetchone()
        assert tuple(media) == ("image", "Canon EOS R5")
    finally:
        conn.close()
    assert names(cfg, "holiday") == {"vacation.jpg"}


def test_video_metadata_is_indexed_and_searchable(cfg, tmp_path: Path):
    cfg.index_media_metadata = True
    root = tmp_path / "docs"
    root.mkdir()
    (root / "clip.mp4").write_bytes((FIXTURES / "sample.mp4").read_bytes())
    run_index(cfg, root)

    assert names(cfg, "Test Video") == {"clip.mp4"}
    conn = db.connect(cfg.db_path, readonly=True)
    try:
        row = conn.execute("SELECT kind, width, height FROM media JOIN files ON files.id = media.file_id "
                           "WHERE files.name = 'clip.mp4'").fetchone()
        assert tuple(row) == ("video", 16, 16)
    finally:
        conn.close()


def test_enabling_media_metadata_reindexes_existing_photos(cfg, tmp_path: Path):
    root = tmp_path / "docs"
    root.mkdir()
    make_jpeg(root / "vacation.jpg", exif={270: "My holiday photo"})
    run_index(cfg, root)  # toggle off: the photo is not indexed at all

    conn = db.connect(cfg.db_path, readonly=True)
    try:
        assert conn.execute("SELECT count(*) FROM files WHERE name = 'vacation.jpg'").fetchone()[0] == 0
    finally:
        conn.close()

    cfg.index_media_metadata = True
    run_index(cfg, root)  # no file on disk changed; only the rules did

    assert names(cfg, "holiday") == {"vacation.jpg"}


def test_disabling_media_metadata_removes_indexed_photos(cfg, tmp_path: Path):
    cfg.index_media_metadata = True
    root = tmp_path / "docs"
    root.mkdir()
    make_jpeg(root / "vacation.jpg", exif={270: "My holiday photo"})
    run_index(cfg, root)
    assert names(cfg, "holiday") == {"vacation.jpg"}

    cfg.index_media_metadata = False
    run_index(cfg, root)

    conn = db.connect(cfg.db_path, readonly=True)
    try:
        assert conn.execute("SELECT count(*) FROM files WHERE name = 'vacation.jpg'").fetchone()[0] == 0
    finally:
        conn.close()


def test_retry_of_failed_photo_stays_in_meta_stage(cfg, tmp_path: Path):
    cfg.index_media_metadata = True
    root = tmp_path / "docs"
    root.mkdir()
    (root / "broken.jpg").write_bytes(b"not a real image, but has the right extension")
    run_index(cfg, root)

    conn = db.connect(cfg.db_path)
    db.init(conn)
    assert conn.execute("SELECT status FROM files WHERE name = 'broken.jpg'").fetchone()[0] == "error"

    control.request_retry(cfg, "error")
    idx = Indexer(cfg, [str(root)], limits=False)
    idx.conn = conn
    idx._take_requests()

    row = conn.execute("SELECT stage, priority FROM queue WHERE path LIKE '%broken.jpg'").fetchone()
    assert tuple(row) == (db.STAGE_META, db.PRIORITY_FORCED)
    conn.close()


def test_reindex_request_for_a_single_video_uses_meta_stage(cfg, tmp_path: Path):
    cfg.index_media_metadata = True
    root = tmp_path / "docs"
    root.mkdir()
    video = root / "clip.mp4"
    video.write_bytes((FIXTURES / "sample.mp4").read_bytes())
    run_index(cfg, root)

    conn = db.connect(cfg.db_path)
    db.init(conn)
    control.request_reindex(cfg, str(video))
    idx = Indexer(cfg, [str(root)], limits=False)
    idx.conn = conn
    idx._take_requests()

    row = conn.execute("SELECT stage, priority FROM queue WHERE path = ?", (str(video),)).fetchone()
    assert tuple(row) == (db.STAGE_META, db.PRIORITY_FORCED)
    conn.close()
