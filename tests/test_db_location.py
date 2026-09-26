"""Where index.db lives (db_dir) and the refusal to put it somewhere SQLite cannot work."""

import json

import pytest

from sindex import config, control, db
from sindex.indexer import EXIT_CONFIG, EXIT_OK, Indexer
from sindex.usn import is_local_folder


def test_a_folder_on_a_local_drive_is_accepted(tmp_path):
    db.check_location(tmp_path / "index.db")


def test_a_missing_folder_is_refused_and_not_created(tmp_path):
    with pytest.raises(db.DbLocationError, match="does not exist"):
        db.check_location(tmp_path / "nope" / "index.db")
    assert not (tmp_path / "nope").exists()


def test_a_folder_that_is_not_on_a_local_drive_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "is_local_folder", lambda path: False)  # a real share cannot be assumed to exist

    with pytest.raises(db.DbLocationError, match="not on a local drive"):
        db.check_location(tmp_path / "index.db")


def test_local_and_network_paths_are_told_apart(tmp_path):
    assert is_local_folder(str(tmp_path))
    assert not is_local_folder(r"\\no-such-server\share\folder")


def test_the_index_is_created_in_db_dir_and_measured_there(cfg, tmp_path):
    elsewhere = tmp_path / "fast-disk"
    elsewhere.mkdir()
    cfg.db_dir = str(elsewhere)
    root = tmp_path / "docs"
    root.mkdir()
    (root / "a.txt").write_text("hello location", encoding="utf-8")

    assert Indexer(cfg, [str(root)], until_idle=True, limits=False).run() == EXIT_OK

    assert (elsewhere / "index.db").exists()
    assert not (cfg.data_dir / "index.db").exists()
    conn = db.connect(cfg.db_path, readonly=True)
    assert control.index_stats(conn, cfg)["db_mb"] > 0
    assert conn.execute("SELECT count(*) FROM files").fetchone()[0] == 1  # and the index folder was not indexed


def test_a_bad_db_dir_stops_the_indexer_with_a_visible_error(cfg, tmp_path):
    cfg.db_dir = str(tmp_path / "gone")

    assert Indexer(cfg, [str(tmp_path)], until_idle=True, limits=False).run() == EXIT_CONFIG

    status = json.loads((cfg.data_dir / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == "error" and "does not exist" in status["detail"]


def test_a_typo_in_a_schedule_window_in_the_file_is_refused_at_load(tmp_path):
    config.load(tmp_path)
    path = tmp_path / "config.toml"
    path.write_text(path.read_text(encoding="utf-8").replace("schedule_windows = []", 'schedule_windows = ["22-06"]'),
                    encoding="utf-8")

    with pytest.raises(config.ConfigError, match="22-06"):
        config.load(tmp_path)
