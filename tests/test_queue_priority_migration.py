"""Files queued by an older sindex (before easy/hard text priorities existed) must be corrected once."""

import time

from sindex import db
from sindex.indexer import Indexer


def _seed(conn, path: str, priority: int, reason: str = "scan") -> None:
    conn.execute("INSERT INTO queue VALUES (?, ?, ?, ?, ?)",
                 (path, db.STAGE_TEXT, priority, reason, time.time()))


def test_migrate_queue_priorities_fixes_stale_hard_files(cfg, tmp_path):
    conn = db.connect(cfg.db_path)
    db.init(conn)
    # simulates rows left over from before this priority split: every text-stage file at PRIORITY_TEXT_EASY
    _seed(conn, str(tmp_path / "notes.txt"), db.PRIORITY_TEXT_EASY)
    _seed(conn, str(tmp_path / "manual.pdf"), db.PRIORITY_TEXT_EASY)
    _seed(conn, str(tmp_path / "urgent.pdf"), db.PRIORITY_FORCED, reason="force")

    idx = Indexer(cfg, [str(tmp_path)], limits=False)
    idx.conn = conn
    idx._migrate_queue_priorities()

    rows = dict(conn.execute("SELECT path, priority FROM queue").fetchall())
    assert rows[str(tmp_path / "notes.txt")] == db.PRIORITY_TEXT_EASY
    assert rows[str(tmp_path / "manual.pdf")] == db.PRIORITY_TEXT_HARD
    assert rows[str(tmp_path / "urgent.pdf")] == db.PRIORITY_FORCED  # a forced request is left alone
    assert db.get_meta(conn, "queue_priorities_migrated") is True
    conn.close()


def test_migrate_queue_priorities_runs_only_once(cfg, tmp_path):
    conn = db.connect(cfg.db_path)
    db.init(conn)
    _seed(conn, str(tmp_path / "manual.pdf"), db.PRIORITY_TEXT_EASY)

    idx = Indexer(cfg, [str(tmp_path)], limits=False)
    idx.conn = conn
    idx._migrate_queue_priorities()
    # simulate a fresh (correctly classified) hard file re-queued at the old, easy priority by hand;
    # a second migration pass must be a no-op so it is not mistaken for a hard file that needs fixing
    conn.execute("UPDATE queue SET priority = ? WHERE path = ?",
                 (db.PRIORITY_TEXT_EASY, str(tmp_path / "manual.pdf")))
    idx._migrate_queue_priorities()

    assert conn.execute("SELECT priority FROM queue WHERE path = ?",
                        (str(tmp_path / "manual.pdf"),)).fetchone()[0] == db.PRIORITY_TEXT_EASY
    conn.close()
