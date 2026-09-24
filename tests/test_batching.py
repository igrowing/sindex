"""Write batching: fewer commits, and consistency when a batch is interrupted."""

import pytest

from sindex import db
from sindex.indexer import Indexer
from sindex.search import search
from sindex.throttle import Stopped


@pytest.fixture
def small_tree(tmp_path):
    root = tmp_path / "many"
    root.mkdir()
    for i in range(30):
        (root / f"f{i:02d}.txt").write_text(f"document number {i} mentions kumquat{i}", encoding="utf-8")
    return root


def run(cfg, root):
    return Indexer(cfg, [str(root)], until_idle=True, limits=False).run()


def counts(cfg):
    conn = db.connect(cfg.db_path, readonly=True)
    try:
        return (conn.execute("SELECT count(*) FROM files").fetchone()[0],
                conn.execute("SELECT count(*) FROM queue").fetchone()[0])
    finally:
        conn.close()


def found(cfg, word):
    conn = db.connect(cfg.db_path, readonly=True)
    try:
        return {r["name"] for r in search(conn, word)["results"]}
    finally:
        conn.close()


def test_commits_are_batched(cfg, small_tree, monkeypatch):
    commits = []
    real = Indexer._commit

    def counting(self):
        if self.in_txn:
            commits.append(self.batch_files)
        real(self)

    monkeypatch.setattr(Indexer, "_commit", counting)
    cfg.commit_every_files = 10
    assert run(cfg, small_tree) == 0
    assert counts(cfg) == (30, 0)
    assert 3 <= len(commits) <= 6, commits  # 30 files in batches of 10 (plus the scan)
    assert max(commits) <= 10


def test_batch_is_committed_when_idle(cfg, small_tree):
    cfg.commit_every_files = 10_000  # never reached: only the idle/exit commit can persist it
    cfg.commit_every_seconds = 10_000
    run(cfg, small_tree)
    assert counts(cfg) == (30, 0)
    assert found(cfg, "kumquat7") == {"f07.txt"}


def test_interrupted_file_is_removed_and_redone(cfg, small_tree, monkeypatch):
    cfg.commit_every_files = 10_000
    real = Indexer._store_text
    calls = {"n": 0}

    def interrupted(self, file_id, path, kind, ctx):
        calls["n"] += 1
        if calls["n"] == 3:
            self.conn.execute("INSERT INTO chunks (rowid, body, source) VALUES (?, 'partial', 'text')",
                              (file_id << db.CHUNK_BITS,))
            raise Stopped
        return real(self, file_id, path, kind, ctx)

    monkeypatch.setattr(Indexer, "_store_text", interrupted)
    assert run(cfg, small_tree) == 0  # Stopped is a clean exit
    files, queued = counts(cfg)
    assert files == 2, "finished files are committed, the interrupted one is not left behind"
    assert queued == 28, "the interrupted file and the unprocessed ones are still queued"
    assert found(cfg, "partial") == set()

    monkeypatch.setattr(Indexer, "_store_text", real)
    assert run(cfg, small_tree) == 0
    assert counts(cfg) == (30, 0)
    assert found(cfg, "kumquat29") == {"f29.txt"}


def test_unexpected_error_discards_only_the_open_batch(cfg, small_tree, monkeypatch):
    cfg.commit_every_files = 10_000
    real = Indexer._poll_journals
    state = {"boom": True}

    def failing(self):
        if state["boom"] and self.batch_files:
            raise RuntimeError("boom")
        return real(self)

    monkeypatch.setattr(Indexer, "_poll_journals", failing)
    with pytest.raises(RuntimeError):
        run(cfg, small_tree)
    files, queued = counts(cfg)
    assert files == 0 and queued == 30, "the batch was rolled back and stays queued"

    state["boom"] = False
    assert run(cfg, small_tree) == 0
    assert counts(cfg) == (30, 0)


def test_leftover_indexing_row_is_redone(cfg, small_tree):
    run(cfg, small_tree)
    conn = db.connect(cfg.db_path)
    conn.execute("UPDATE files SET status = 'indexing' WHERE name = 'f05.txt'")
    db.enqueue(conn, str(small_tree / "f05.txt"), reason="test")
    conn.close()
    run(cfg, small_tree)
    conn = db.connect(cfg.db_path, readonly=True)
    assert conn.execute("SELECT status FROM files WHERE name = 'f05.txt'").fetchone()[0] == "ok"
