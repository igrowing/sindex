"""The indexer as the supervisor starts it: a separate process whose stdin is a pipe it watches."""

import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from sindex import db

PROJECT_ROOT = Path(__file__).resolve().parents[1]

CONFIG = """\
[throttle]
extract_workers = 2
duty_factor = 0
read_mb_per_sec = 0
pause_on_battery = false
pause_when_cpu_busy_percent = 0
"""


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "many"
    root.mkdir()
    for i in range(30):
        (root / f"f{i:02d}.txt").write_text(f"document number {i} mentions kumquat{i}", encoding="utf-8")
    return root


@pytest.fixture
def launch(cfg, tmp_path):
    (cfg.data_dir / "config.toml").write_text(CONFIG, encoding="utf-8")
    procs = []

    def start(root, *extra):
        stderr = (tmp_path / "indexer-stderr.log").open("wb")
        proc = subprocess.Popen(
            [sys.executable, "-m", "sindex.indexer", "--data-dir", str(cfg.data_dir), "--root", str(root),
             "--stdin-stop", "--no-limits", *extra],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=stderr, cwd=PROJECT_ROOT)
        stderr.close()
        procs.append(proc)
        return proc

    yield start
    for proc in procs:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def file_count(cfg) -> int:
    if not cfg.db_path.exists():
        return 0
    conn = db.connect(cfg.db_path, readonly=True)
    try:
        return conn.execute("SELECT count(*) FROM files WHERE status = 'ok'").fetchone()[0]
    except sqlite3.Error:
        return 0  # the database may not have its tables yet while the indexer starts
    finally:
        conn.close()


def wait_for(condition, seconds: float, what: str) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.25)


def test_workers_start_when_stdin_is_a_pipe(cfg, tree, launch):
    # a thread blocked in a read on the stdin pipe used to stall the start-up of every worker process
    proc = launch(tree, "--until-idle")
    try:
        assert proc.wait(60) == 0
    except subprocess.TimeoutExpired:
        pytest.fail(f"the indexer hung with {file_count(cfg)} of 30 files indexed")
    assert file_count(cfg) == 30


def test_indexer_stops_when_the_supervisor_closes_stdin(cfg, tree, launch):
    proc = launch(tree)
    wait_for(lambda: file_count(cfg) == 30, 60, "the files to be indexed")
    assert proc.poll() is None, "it keeps running when there is nothing to do"
    proc.stdin.close()
    assert proc.wait(20) == 0


def test_stdin_closed_notices_the_writer_going_away():
    code = ("import time\nfrom sindex.throttle import stdin_closed\n"
            "while not stdin_closed():\n    time.sleep(0.05)\nprint('closed')\n")
    proc = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            text=True, cwd=PROJECT_ROOT)
    try:
        time.sleep(1.0)
        assert proc.poll() is None, "an open pipe is not closed"
        proc.stdin.close()
        out, _ = proc.communicate(timeout=20)
    finally:
        proc.kill()
    assert out.strip() == "closed"
