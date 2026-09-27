"""Discovery order: easy (plain text) files are queued ahead of slow formats (PDF, Office, RTF)."""

import threading
from pathlib import Path

from sindex import db, scanner
from sindex.filters import PathFilter
from sindex.throttle import Throttle

from .conftest import make_jpeg, make_pdf


def test_queue_priority_ranks_text_before_hard_formats(cfg):
    assert scanner.queue_priority("notes.txt", cfg) == db.PRIORITY_TEXT_EASY
    assert scanner.queue_priority("report.docx", cfg) == db.PRIORITY_TEXT_HARD
    assert scanner.queue_priority("manual.pdf", cfg) == db.PRIORITY_TEXT_HARD
    assert db.PRIORITY_TEXT_EASY < db.PRIORITY_TEXT_HARD < db.PRIORITY[db.STAGE_META]


def test_queue_priority_force_overrides_kind(cfg):
    assert scanner.queue_priority("manual.pdf", cfg, force=True) == db.PRIORITY_FORCED
    assert scanner.queue_priority("notes.txt", cfg, force=True) == db.PRIORITY_FORCED


def test_scan_queues_easy_files_ahead_of_hard_ones(cfg, tmp_path: Path):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "notes.txt").write_text("plain text", encoding="utf-8")
    make_pdf(root / "manual.pdf", ["hard to parse"])

    conn = db.connect(cfg.db_path)
    db.init(conn)
    filt = PathFilter(cfg, [str(root)])
    throttle = Throttle(cfg, threading.Event())

    queued = scanner.scan(conn, str(root), filt, throttle)

    assert queued == 2
    rows = conn.execute("SELECT path, priority FROM queue ORDER BY priority, enqueued_at").fetchall()
    assert [Path(path).name for path, _ in rows] == ["notes.txt", "manual.pdf"]
    assert {Path(path).name: priority for path, priority in rows} == {
        "notes.txt": db.PRIORITY_TEXT_EASY,
        "manual.pdf": db.PRIORITY_TEXT_HARD,
    }
    conn.close()


def test_queue_target_routes_media_kinds_to_meta_stage(cfg):
    cfg.index_media_metadata = True
    assert scanner.queue_target("photo.jpg", cfg) == (db.STAGE_META, db.PRIORITY_MEDIA)
    assert scanner.queue_target("clip.mp4", cfg) == (db.STAGE_META, db.PRIORITY_MEDIA)


def test_queue_target_falls_back_to_text_stage_and_priority(cfg):
    assert scanner.queue_target("notes.txt", cfg) == (db.STAGE_TEXT, db.PRIORITY_TEXT_EASY)
    assert scanner.queue_target("manual.pdf", cfg) == (db.STAGE_TEXT, db.PRIORITY_TEXT_HARD)
    cfg.index_media_metadata = True  # off by default; still text without the toggle mattering here
    assert scanner.queue_target("notes.txt", cfg) == (db.STAGE_TEXT, db.PRIORITY_TEXT_EASY)


def test_media_priority_sits_between_easy_and_hard_text(cfg):
    assert db.PRIORITY_TEXT_EASY < db.PRIORITY_MEDIA < db.PRIORITY_TEXT_HARD


def test_queue_target_force_goes_to_meta_stage_with_forced_priority(cfg):
    cfg.index_media_metadata = True
    assert scanner.queue_target("photo.jpg", cfg, force=True) == (db.STAGE_META, db.PRIORITY_FORCED)
    assert scanner.queue_target("notes.txt", cfg, force=True) == (db.STAGE_TEXT, db.PRIORITY_FORCED)


def test_scan_queues_media_between_easy_and_hard_text_when_enabled(cfg, tmp_path: Path):
    cfg.index_media_metadata = True
    root = tmp_path / "mixed"
    root.mkdir()
    (root / "notes.txt").write_text("plain text", encoding="utf-8")
    make_jpeg(root / "photo.jpg")
    make_pdf(root / "manual.pdf", ["hard to parse"])

    conn = db.connect(cfg.db_path)
    db.init(conn)
    filt = PathFilter(cfg, [str(root)])
    throttle = Throttle(cfg, threading.Event())

    queued = scanner.scan(conn, str(root), filt, throttle)

    assert queued == 3
    rows = conn.execute("SELECT path, stage, priority FROM queue ORDER BY priority, enqueued_at").fetchall()
    assert [Path(path).name for path, _, _ in rows] == ["notes.txt", "photo.jpg", "manual.pdf"]
    assert {Path(path).name: stage for path, stage, _ in rows} == {
        "notes.txt": db.STAGE_TEXT, "photo.jpg": db.STAGE_META, "manual.pdf": db.STAGE_TEXT,
    }
    conn.close()


def test_scan_does_not_queue_media_files_when_the_toggle_is_off(cfg, tmp_path: Path):
    root = tmp_path / "mixed"
    root.mkdir()
    make_jpeg(root / "photo.jpg")

    conn = db.connect(cfg.db_path)
    db.init(conn)
    filt = PathFilter(cfg, [str(root)])
    throttle = Throttle(cfg, threading.Event())

    queued = scanner.scan(conn, str(root), filt, throttle)

    assert queued == 0
    conn.close()
