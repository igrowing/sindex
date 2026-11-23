"""The data folder: the pointer that names it, the checks before a change, the copy, and the supervisor and web
API that carry it out."""

import json
import threading

import pytest

from sindex import config, control, datadir, db
from sindex.service import Supervisor

from .conftest import call


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A machine whose bootstrap folder is empty: the data folder is the one the pointer file decides."""
    monkeypatch.delenv("SINDEX_HOME", raising=False)
    monkeypatch.setenv("ProgramData", str(tmp_path / "PD"))
    return config.load(config.default_data_dir())


def _make_index(path, name="kept.txt"):
    conn = db.connect(path)
    db.init(conn)
    conn.execute("INSERT INTO files (dir, name, size, mtime_ns, status, indexed_at) VALUES ('d', ?, 1, 1, 'ok', 1)",
                 (name,))
    conn.close()


def _names(path):
    conn = db.connect(path, readonly=True)
    try:
        return [r[0] for r in conn.execute("SELECT name FROM files")]
    finally:
        conn.close()


def _folder(tmp_path, name="new"):
    folder = tmp_path / name
    folder.mkdir()
    return folder


# -- the pointer ----------------------------------------------------------------------------------------------

def test_without_a_pointer_the_data_folder_is_in_programdata(home, tmp_path):
    assert home.data_dir == (tmp_path / "PD" / "sindex").resolve()
    assert config.data_dir_is_movable(home)


def test_the_pointer_names_the_data_folder_of_the_next_start(home, tmp_path):
    config.set_data_dir_pointer(tmp_path / "elsewhere")

    assert config.default_data_dir() == tmp_path / "elsewhere"
    assert config.load().data_dir == (tmp_path / "elsewhere").resolve()


def test_pointing_back_to_programdata_removes_the_pointer(home, tmp_path):
    config.set_data_dir_pointer(tmp_path / "elsewhere")

    config.set_data_dir_pointer(config.bootstrap_dir())

    assert not (config.bootstrap_dir() / config.POINTER_NAME).exists()
    assert config.default_data_dir() == config.bootstrap_dir()


@pytest.mark.parametrize("text", ["", "relative\\folder"])
def test_a_pointer_that_is_not_a_full_path_stops_the_start_instead_of_starting_an_empty_index(home, text):
    (config.bootstrap_dir() / config.POINTER_NAME).write_text(text, encoding="utf-8")

    with pytest.raises(config.ConfigError, match=config.POINTER_NAME):
        config.default_data_dir()


def test_sindex_home_wins_over_the_pointer_and_cannot_be_moved_from_the_ui(home, tmp_path, monkeypatch):
    config.set_data_dir_pointer(tmp_path / "elsewhere")
    monkeypatch.setenv("SINDEX_HOME", str(tmp_path / "forced"))

    assert config.default_data_dir() == tmp_path / "forced"
    assert not config.data_dir_is_movable(home)


def test_a_data_folder_given_on_the_command_line_cannot_be_moved_from_the_ui(cfg, tmp_path):
    with pytest.raises(datadir.DataFolderError, match="--data-dir"):
        datadir.plan(cfg, str(_folder(tmp_path)), "copy")


# -- the checks ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize(("folder", "mode", "message"), [
    ("relative\\folder", "copy", "full path"),
    ("{data}", "copy", "already"),
    ("{missing}", "copy", "does not exist"),
    ("{empty}", "use", "no config.toml or index.db"),
    ("{empty}", "swap", "unknown"),
])
def test_a_bad_request_is_refused_before_anything_changes(home, tmp_path, folder, mode, message):
    folder = folder.format(data=home.data_dir, missing=tmp_path / "gone", empty=_folder(tmp_path, "empty"))

    with pytest.raises(datadir.DataFolderError, match=message):
        datadir.plan(home, folder, mode)


def test_a_folder_that_is_not_on_a_local_drive_is_refused(home, tmp_path, monkeypatch):
    monkeypatch.setattr(db, "is_local_folder", lambda path: False)

    with pytest.raises(datadir.DataFolderError, match="not on a local drive"):
        datadir.plan(home, str(_folder(tmp_path)), "copy")


def test_copying_onto_data_that_is_there_already_is_refused(home, tmp_path):
    folder = _folder(tmp_path)
    _make_index(folder / "index.db")

    with pytest.raises(datadir.DataFolderError, match="already"):
        datadir.plan(home, str(folder), "copy")


def test_using_a_file_that_is_not_an_index_is_refused(home, tmp_path):
    garbage = _folder(tmp_path, "garbage")
    (garbage / "index.db").write_bytes(b"this is not a database" * 100)
    other = _folder(tmp_path, "other")
    conn = db.connect(other / "index.db")
    conn.execute("CREATE TABLE unrelated (x)")
    conn.close()

    with pytest.raises(datadir.DataFolderError, match="cannot be read"):
        datadir.plan(home, str(garbage), "use")
    with pytest.raises(datadir.DataFolderError, match="not an index"):
        datadir.plan(home, str(other), "use")


def test_using_an_index_of_a_newer_schema_is_refused(home, tmp_path):
    newer = _folder(tmp_path, "newer")
    conn = db.connect(newer / "index.db")
    db.init(conn)
    db.set_meta(conn, "schema_version", db.SCHEMA_VERSION + 1)
    conn.close()

    with pytest.raises(datadir.DataFolderError, match="newer"):
        datadir.plan(home, str(newer), "use")


def test_using_settings_that_cannot_be_parsed_is_refused(home, tmp_path):
    folder = _folder(tmp_path)
    (folder / "config.toml").write_text("this is [not toml", encoding="utf-8")

    with pytest.raises(datadir.DataFolderError, match="cannot be read"):
        datadir.plan(home, str(folder), "use")


# -- the switch ---------------------------------------------------------------------------------------------

def test_copying_takes_the_settings_and_the_index_and_leaves_the_old_folder_alone(home, tmp_path):
    _make_index(home.db_path)
    home.duty_factor = 0.5
    config.save(home)
    folder = _folder(tmp_path)

    assert datadir.switch(home, str(folder), "copy") == folder

    assert _names(folder / "index.db") == ["kept.txt"]
    assert config.load(folder).duty_factor == 0.5
    assert _names(home.db_path) == ["kept.txt"] and (home.data_dir / "config.toml").exists()
    assert not list(folder.glob("*.partial"))
    assert config.default_data_dir() == folder


def test_an_index_kept_elsewhere_by_db_dir_is_not_copied(home, tmp_path):
    away = _folder(tmp_path, "away")
    home.db_dir = str(away)
    config.save(home)
    _make_index(home.db_path)
    folder = _folder(tmp_path)

    datadir.switch(home, str(folder), "copy")

    assert not (folder / "index.db").exists()
    assert config.load(folder).db_path == away / "index.db"


def test_using_a_prebuilt_index_changes_only_the_pointer(home, tmp_path):
    _make_index(home.db_path)
    built = _folder(tmp_path, "built")
    _make_index(built / "index.db", "prebuilt.txt")

    datadir.switch(home, str(built), "use")

    assert config.default_data_dir() == built
    assert _names(home.db_path) == ["kept.txt"]
    assert not (built / "config.toml").exists()  # the next start writes settings of its own


def test_a_copy_needs_the_free_space_and_leaves_nothing_behind(home, tmp_path, monkeypatch):
    _make_index(home.db_path)
    folder = _folder(tmp_path)
    monkeypatch.setattr(datadir.shutil, "disk_usage", lambda path: type("U", (), {"free": 10})())

    with pytest.raises(datadir.DataFolderError, match="free"):
        datadir.switch(home, str(folder), "copy")

    assert not list(folder.iterdir())
    assert config.default_data_dir() == home.data_dir


def test_a_failed_copy_removes_what_it_copied(home, tmp_path, monkeypatch):
    _make_index(home.db_path)
    folder = _folder(tmp_path)
    real = datadir.shutil.copyfile

    def fail_on_the_index(src, dst):
        if str(dst).startswith(str(folder / "index.db")):
            raise OSError("disk gone")
        return real(src, dst)

    monkeypatch.setattr(datadir.shutil, "copyfile", fail_on_the_index)

    with pytest.raises(datadir.DataFolderError, match="disk gone"):
        datadir.switch(home, str(folder), "copy")

    assert not list(folder.iterdir())  # config.toml was copied first and is taken back
    assert not (config.bootstrap_dir() / config.POINTER_NAME).exists()


# -- the supervisor and the web API ---------------------------------------------------------------------------

def test_supervisor_switches_the_data_folder_between_two_indexers(home, tmp_path):
    _make_index(home.db_path)
    folder = _folder(tmp_path)
    sup = Supervisor(home, threading.Event())
    control.request_data_folder(home, str(folder), "copy")

    assert sup._apply_settings() is True
    assert sup.moved_to is None  # nothing is copied while the indexer may still run
    sup._switch_data_folder()

    assert sup.moved_to == folder and sup.settings_error is None
    assert _names(folder / "index.db") == ["kept.txt"]
    assert config.default_data_dir() == folder


def test_supervisor_does_not_start_an_indexer_after_the_switch(home, tmp_path):
    folder = _folder(tmp_path)
    stop = threading.Event()
    sup = Supervisor(home, stop)
    sup._spawn = lambda: pytest.fail("an indexer was started for the old folder")
    control.request_data_folder(home, str(folder), "copy")
    sup._apply_settings()
    thread = threading.Thread(target=sup.run, daemon=True)  # ends with `stop`, which the test sets
    thread.start()
    try:
        thread.join(1.0)
        assert thread.is_alive() and sup.moved_to == folder  # waiting for the service to be restarted
    finally:
        stop.set()
        thread.join(5.0)
    assert not thread.is_alive()


def test_supervisor_refuses_a_bad_request_and_changes_nothing(home, tmp_path):
    sup = Supervisor(home, threading.Event())
    control.request_data_folder(home, str(_folder(tmp_path, "empty")), "use")  # no data in it

    assert sup._apply_settings() is False
    assert "no config.toml or index.db" in sup.settings_error

    sup._switch_data_folder()  # nothing was queued
    assert sup.moved_to is None


def test_a_malformed_request_is_reported(home):
    sup = Supervisor(home, threading.Event())
    control.request_settings(home, {"data_folder": r"D:\Data"})

    assert sup._apply_settings() is False

    assert "data_folder" in sup.settings_error


def test_the_api_queues_a_data_folder_change_for_the_supervisor(web, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "data_dir_is_movable", lambda cfg: True)
    folder = _folder(tmp_path)

    status, _ = call(web, "/api/data-folder", {"folder": str(folder), "mode": "copy"}, token="secret")

    assert status == 200
    assert control.take_settings_requests(web) == [{"data_folder": {"folder": str(folder), "mode": "copy"}}]


def test_the_api_refuses_a_bad_folder_with_the_reason(web, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "data_dir_is_movable", lambda cfg: True)

    status, body = call(web, "/api/data-folder", {"folder": str(tmp_path / "gone"), "mode": "copy"}, token="secret")

    assert status == 400
    assert "does not exist" in json.loads(body)["error"]
    assert control.take_settings_requests(web) == []


def test_the_api_needs_the_token_for_a_data_folder_change(web, tmp_path):
    status, _ = call(web, "/api/data-folder", {"folder": str(tmp_path), "mode": "copy"})

    assert status == 403
    assert control.take_settings_requests(web) == []


def test_the_status_tells_whether_the_data_folder_can_be_changed(web):
    _make_index(web.db_path)

    status, body = call(web, "/api/status")

    index = json.loads(body)["index"]
    assert (status, index["data_dir"], index["data_dir_movable"]) == (200, str(web.data_dir), False)
