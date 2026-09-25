import re

from sindex import cli, db


def test_index_command_recycles_in_fresh_processes(tmp_path):
    # `sindex index` used to "recycle" by making a new Indexer inside the same process, which frees
    # no memory: the process kept growing until the job memory cap made every allocation fail
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.toml").write_text(
        "[throttle]\nrecycle_after_files = 3\nduty_factor = 0\nread_mb_per_sec = 0\n"
        "pause_on_battery = false\npause_when_cpu_busy_percent = 0\n", encoding="utf-8")
    root = tmp_path / "docs"
    root.mkdir()
    for i in range(10):
        (root / f"f{i}.txt").write_text(f"document number {i}", encoding="utf-8")

    code = cli.main(["--data-dir", str(data), "index", "--root", str(root), "--no-limits"])

    assert code == 0
    conn = db.connect(data / "index.db", readonly=True)
    assert conn.execute("SELECT count(*) FROM files").fetchone()[0] == 10
    assert conn.execute("SELECT count(*) FROM queue").fetchone()[0] == 0
    log = (data / "logs" / "indexer.log").read_text(encoding="utf-8")
    pids = set(re.findall(r"indexer\[(\d+)\]", log))
    assert len(pids) >= 3, f"10 files with a recycle every 3 should take at least 3 processes, saw {pids}"
