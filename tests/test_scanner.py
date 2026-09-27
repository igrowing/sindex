"""Discovery order: easy (plain text) files are queued ahead of slow formats (PDF, Office, RTF)."""

import threading
from pathlib import Path

from sindex import db, scanner
from sindex.filters import PathFilter
from sindex.throttle import Throttle

from .conftest import make_pdf


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
