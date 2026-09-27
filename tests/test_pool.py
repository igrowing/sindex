"""The pool of extraction worker processes, and the indexer running on top of it."""

import logging
import threading
import time

import pytest

from sindex import config, db
from sindex import indexer as indexer_module
from sindex.extract.job import Chunk, Result, Task
from sindex.extract.pool import ProcessPool, make_runner
from sindex.indexer import Indexer
from sindex.search import search
from sindex.throttle import Throttle

from .conftest import make_docx, make_jpeg, make_pdf


@pytest.fixture
def make_pool(cfg):
    pools = []

    def make(size=2, **overrides):
        for key, value in overrides.items():
            setattr(cfg, key, value)
        pool = ProcessPool(cfg, size, cfg.data_dir / "paused", Throttle(cfg, threading.Event()))
        pools.append(pool)
        return pool

    yield make
    for pool in pools:
        pool.close()


def text_file(tmp_path, name="a.txt", text="the quick brown fox"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def collect(pool, jobs, timeout=60.0):
    """Poll until `jobs` results have arrived. Returns the chunk texts and the result of each job."""
    chunks: dict[int, list[str]] = {}
    results: dict[int, Result] = {}
    deadline = time.monotonic() + timeout
    while len(results) < jobs:
        assert time.monotonic() < deadline, "the workers did not answer in time"
        for msg in pool.poll(0.5):
            if isinstance(msg, Chunk):
                chunks.setdefault(msg.job, []).append(msg.text)
            else:
                results[msg.job] = msg
    return chunks, results


def test_workers_run_side_by_side(make_pool, tmp_path):
    pool = make_pool(2)
    a, b = text_file(tmp_path, "a.txt", "alpha"), text_file(tmp_path, "b.txt", "beta")
    pool.submit(Task(1, str(a), "text", 10))
    assert pool.has_slot(), "a second worker can still be started"
    pool.submit(Task(2, str(b), "text", 10))
    assert not pool.has_slot()
    pids = {w.process.pid for w in pool._workers}
    assert len(pids) == 2, "two files in flight are two different processes"
    chunks, results = collect(pool, 2)
    assert chunks == {1: ["alpha"], 2: ["beta"]}
    assert {r.status for r in results.values()} == {"ok"}
    assert pool.has_slot(), "workers are free again once their results were read"


def test_result_media_field_survives_the_process_pool_round_trip(make_pool, tmp_path):
    pool = make_pool(1)
    photo = make_jpeg(tmp_path / "photo.jpg", exif={271: "Canon", 272: "EOS R5"}, size=(6, 4))
    pool.submit(Task(1, str(photo), "image", 10))
    _, results = collect(pool, 1)
    media = results[1].media
    assert media is not None and (media.width, media.height) == (6, 4) and media.camera == "Canon EOS R5"


def test_a_worker_takes_the_next_file_after_the_first(make_pool, tmp_path):
    pool = make_pool(1)
    pool.submit(Task(1, str(text_file(tmp_path, "a.txt")), "text", 10))
    collect(pool, 1)
    pid = pool._workers[0].process.pid
    pool.submit(Task(2, str(text_file(tmp_path, "b.txt")), "text", 10))
    _, results = collect(pool, 1)
    assert results[2].status == "ok"
    assert [w.process.pid for w in pool._workers] == [pid], "one worker serves both files"


def test_a_worker_that_dies_costs_only_its_file(make_pool, tmp_path):
    pool = make_pool(1, read_mb_per_sec=0.05)  # 50 KB/s: the big file below keeps the worker busy for seconds
    pool.submit(Task(1, str(text_file(tmp_path)), "text", 10))
    collect(pool, 1)  # the worker is up and running
    big = text_file(tmp_path, "big.txt", "word " * 80_000)
    pool.submit(Task(2, str(big), "text", 10))
    pool._workers[0].process.kill()
    _, results = collect(pool, 1)
    assert results[2].status == "error" and "died" in results[2].error
    assert not pool._workers

    pool.submit(Task(3, str(text_file(tmp_path, "c.txt")), "text", 10))
    _, results = collect(pool, 1)
    assert results[3].status == "ok", "the pool started a new worker"


def test_a_worker_that_dies_while_starting_is_noticed_too(make_pool, tmp_path):
    pool = make_pool(1)
    pool.submit(Task(1, str(text_file(tmp_path)), "text", 10))
    pool._workers[0].process.kill()  # before it has read anything, not even its pipe
    _, results = collect(pool, 1, timeout=20)
    assert results[1].status == "error" and "died" in results[1].error


def test_a_worker_that_grew_is_replaced(make_pool, tmp_path):
    pool = make_pool(1, recycle_after_files=1)
    f = text_file(tmp_path)
    pool.submit(Task(1, str(f), "text", 10))
    _, results = collect(pool, 1)
    assert results[1].recycle and not pool._workers, "the old worker was retired"
    pool.submit(Task(2, str(f), "text", 10))
    _, results = collect(pool, 1)
    assert results[2].status == "ok"


def test_close_leaves_no_process_behind(make_pool, tmp_path):
    pool = make_pool(2)
    f = text_file(tmp_path)
    pool.submit(Task(1, str(f), "text", 10))
    pool.submit(Task(2, str(f), "text", 10))
    processes = [w.process for w in pool._workers]
    pool.close()
    assert not any(p.is_alive() for p in processes)
    assert not pool._workers


def test_read_limit_is_shared_between_workers(cfg, make_pool):
    pool = make_pool(4, read_mb_per_sec=8.0)
    assert pool._cfg.read_mb_per_sec == 2.0
    assert cfg.read_mb_per_sec == 8.0, "the indexer's own config is untouched"


def test_make_runner_follows_the_worker_count(cfg):
    cfg.extract_workers = 0
    assert not isinstance(make_runner(cfg, Throttle(cfg, threading.Event()), cfg.data_dir / "paused"), ProcessPool)
    cfg.extract_workers = 2
    assert isinstance(make_runner(cfg, Throttle(cfg, threading.Event()), cfg.data_dir / "paused"), ProcessPool)


# ---- the indexer on top of the pool ------------------------------------------------------------


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    for i in range(30):
        (root / f"f{i:02d}.txt").write_text(f"document number {i} mentions kumquat{i}", encoding="utf-8")
    make_pdf(root / "manual.pdf", ["Installation manual", "Turn the device on"])
    make_docx(root / "report.docx", ["Quarterly report", "Revenue grew in Москва"])
    return root


def names(cfg, query):
    conn = db.connect(cfg.db_path, readonly=True)
    try:
        return {r["name"] for r in search(conn, query)["results"]}
    finally:
        conn.close()


def scalar(cfg, sql):
    conn = db.connect(cfg.db_path, readonly=True)
    try:
        return conn.execute(sql).fetchone()[0]
    finally:
        conn.close()


def test_indexer_with_workers_indexes_everything(cfg, tree, monkeypatch):
    cfg.extract_workers = 2
    spawned = []
    real = ProcessPool._spawn

    def counting(self):
        spawned.append(1)
        return real(self)

    monkeypatch.setattr(ProcessPool, "_spawn", counting)
    assert Indexer(cfg, [str(tree)], until_idle=True, limits=False).run() == 0
    assert len(spawned) == 2, "two workers, started when the second file was in flight"
    assert scalar(cfg, "SELECT count(*) FROM files") == 32
    assert scalar(cfg, "SELECT count(*) FROM queue") == 0
    assert scalar(cfg, "SELECT count(*) FROM files WHERE status != 'ok'") == 0
    assert names(cfg, "kumquat7") == {"f07.txt"}
    assert names(cfg, "manual") == {"manual.pdf"}
    assert names(cfg, "москва") == {"report.docx"}


def test_stopping_with_files_in_flight_leaves_no_half_file(cfg, tree, monkeypatch):
    cfg.extract_workers = 2
    cfg.commit_every_files = 10_000
    real = Indexer._on_message

    def stopping(self, msg):
        real(self, msg)
        if isinstance(msg, Result) and self.files_done >= 5:
            self.stop.set()

    monkeypatch.setattr(Indexer, "_on_message", stopping)
    assert Indexer(cfg, [str(tree)], stop=threading.Event(), limits=False).run() == 0
    done, queued = scalar(cfg, "SELECT count(*) FROM files"), scalar(cfg, "SELECT count(*) FROM queue")
    assert done >= 5
    assert scalar(cfg, "SELECT count(*) FROM files WHERE status = 'indexing'") == 0
    assert done + queued == 32, "every file is either finished or still queued"

    monkeypatch.setattr(Indexer, "_on_message", real)
    assert Indexer(cfg, [str(tree)], until_idle=True, limits=False).run() == 0
    assert scalar(cfg, "SELECT count(*) FROM files") == 32
    assert scalar(cfg, "SELECT count(*) FROM queue") == 0
    assert names(cfg, "kumquat29") == {"f29.txt"}


def test_commit_never_sees_a_file_in_flight(cfg, tree, monkeypatch):
    cfg.extract_workers = 3
    cfg.commit_every_files = 5
    seen = []
    real = Indexer._commit

    def checking(self):
        seen.append((len(self.jobs), self.batch_files))
        real(self)

    monkeypatch.setattr(Indexer, "_commit", checking)
    assert Indexer(cfg, [str(tree)], until_idle=True, limits=False).run() == 0
    assert all(jobs == 0 for jobs, _ in seen)
    assert max(files for _, files in seen) <= 5, "files in flight count towards the batch"


# ---- progress log ------------------------------------------------------------------------------


@pytest.mark.parametrize("workers", [0, 2])
def test_progress_is_logged_every_n_files(cfg, tree, monkeypatch, caplog, workers):
    cfg.extract_workers = workers
    monkeypatch.setattr(indexer_module, "PROGRESS_EVERY", 10)
    with caplog.at_level(logging.INFO, logger="sindex.indexer"):
        assert Indexer(cfg, [str(tree)], until_idle=True, limits=False).run() == 0
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("progress:")]
    assert len(lines) == 3, "32 files, a line after each 10"
    counts = [[int(x) for x in line.split() if x.isdigit()] for line in lines]
    assert [c[0] for c in counts] == sorted(c[0] for c in counts), "files indexed only grows"
    assert [c[1] for c in counts] == sorted((c[1] for c in counts), reverse=True), "the queue only shrinks"
    assert all("files/s" in line for line in lines)


def test_worker_setting_is_read_from_config(tmp_path):
    (tmp_path / "config.toml").write_text("[throttle]\nextract_workers = 5\n", encoding="utf-8")
    assert config.load(tmp_path).extract_workers == 5
    assert config.Config().extract_workers == 3, "the default is on"


def test_quiet_third_party_logs():
    logging.getLogger("pypdf").setLevel(logging.NOTSET)
    config.quiet_third_party_logs()
    assert logging.getLogger("pypdf").level == logging.ERROR
