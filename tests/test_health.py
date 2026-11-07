"""Indexing health (files that ended badly, and retrying them) and the maintenance requests."""

import json
from pathlib import Path

import pytest

from sindex import control, db
from sindex.indexer import Indexer
from sindex.search import search

from .conftest import call


@pytest.fixture
def root(tmp_path: Path) -> Path:
    root = tmp_path / "docs"
    root.mkdir()
    (root / "big.txt").write_text("elephant " * 100, encoding="utf-8")
    (root / "small.txt").write_text("mouse", encoding="utf-8")
    (root / "blob.txt").write_bytes(b"\x00\x01\x02 binary \x00")
    (root / "broken.pdf").write_bytes(b"this is not a pdf")
    return root


def run(cfg, root: Path) -> None:
    assert Indexer(cfg, [str(root)], until_idle=True, limits=False).run() == 0


def found(cfg, word: str) -> set[str]:
    conn = db.connect(cfg.db_path, readonly=True)
    try:
        return {r["name"] for r in search(conn, word)["results"]}
    finally:
        conn.close()


def statuses(cfg) -> dict[str, str]:
    conn = db.connect(cfg.db_path, readonly=True)
    try:
        return dict(conn.execute("SELECT name, status FROM files"))
    finally:
        conn.close()


def test_problem_files_are_listed_by_status_with_their_error(cfg, root):
    cfg.max_text_mb = 0  # every text file is too big
    run(cfg, root)
    conn = db.connect(cfg.db_path, readonly=True)

    too_big = control.problem_files(conn, "too_big")
    errors = control.problem_files(conn, "error")

    assert [f["name"] for f in too_big] == ["big.txt", "blob.txt", "small.txt"]
    assert too_big[0]["path"] == str(root / "big.txt") and too_big[0]["size"] == 900
    assert [f["name"] for f in errors] == ["broken.pdf"] and errors[0]["error"]
    assert [f["name"] for f in control.problem_files(conn, "too_big", limit=1, offset=1)] == ["blob.txt"]
    with pytest.raises(ValueError):
        control.problem_files(conn, "ok")  # healthy files are not a problem list


def test_retrying_too_big_files_after_raising_the_limit(cfg, root):
    cfg.max_text_mb = 0
    run(cfg, root)
    assert found(cfg, "elephant") == set()

    cfg.max_text_mb = 50
    run(cfg, root)
    assert found(cfg, "elephant") == set()  # unchanged files are not read again by themselves

    control.request_retry(cfg, "too_big")
    run(cfg, root)

    assert found(cfg, "elephant") == {"big.txt"} and found(cfg, "mouse") == {"small.txt"}
    assert statuses(cfg)["big.txt"] == "ok" and statuses(cfg)["blob.txt"] == "binary"
    assert statuses(cfg)["broken.pdf"] == "error"  # only the asked status was retried... and it still fails


def test_a_retry_request_for_a_status_that_is_not_a_problem_is_refused(cfg):
    with pytest.raises(ValueError):
        control.request_retry(cfg, "ok")
    assert not (cfg.data_dir / "requests").exists()


def test_rebuild_reads_every_file_again(cfg, root):
    run(cfg, root)
    conn = db.connect(cfg.db_path)
    conn.execute("DELETE FROM chunks")  # the index lost its text; the files still look unchanged
    conn.close()
    run(cfg, root)
    assert found(cfg, "elephant") == set()

    control.request_rebuild(cfg)
    run(cfg, root)

    assert found(cfg, "elephant") == {"big.txt"}


def test_maintenance_runs_when_the_queue_is_empty_and_is_reported(cfg, root):
    run(cfg, root)
    control.request_maintenance(cfg, "optimize")
    control.request_maintenance(cfg, "vacuum")
    control.request_maintenance(cfg, "vacuum")  # asking twice does not do it twice

    run(cfg, root)

    conn = db.connect(cfg.db_path, readonly=True)
    stats = control.index_stats(conn, cfg)
    assert stats["maintenance_pending"] == []
    assert stats["last_maintenance"]["action"] == "vacuum"
    assert stats["last_maintenance"]["mb_after"] > 0 and stats["last_maintenance"]["seconds"] >= 0
    assert found(cfg, "elephant") == {"big.txt"}  # the index still works


def test_an_unknown_maintenance_action_is_refused(cfg):
    with pytest.raises(ValueError):
        control.request_maintenance(cfg, "drop-everything")


def test_maintenance_waits_for_the_end_of_the_queue(cfg, root):
    run(cfg, root)
    conn = db.connect(cfg.db_path)
    db.set_meta(conn, "maintenance_pending", ["optimize"])
    db.enqueue(conn, str(root / "small.txt"))
    conn.close()
    indexer = Indexer(cfg, [str(root)], until_idle=True, limits=False)
    queued_when_asked = []
    original = indexer._run_maintenance

    def spy() -> bool:
        queued_when_asked.append(indexer.conn.execute("SELECT count(*) FROM queue").fetchone()[0])
        return original()

    indexer._run_maintenance = spy

    assert indexer.run() == 0

    assert queued_when_asked[0] == 0  # first asked for only once nothing was left in the queue


def test_health_api_counts_lists_and_pages(web, root):
    web.max_text_mb = 0
    run(web, root)

    status, body = call(web, "/api/health")
    data = json.loads(body)
    assert status == 200 and data["files"] == []
    assert data["counts"] == {"error": 1, "too_big": 3, "binary": 0, "truncated": 0, "empty": 0}

    data = json.loads(call(web, "/api/health?status=too_big&offset=1")[1])
    assert [f["name"] for f in data["files"]] == ["blob.txt", "small.txt"]

    assert call(web, "/api/health?status=ok")[0] == 400
    assert call(web, "/api/health?status=error&offset=x")[0] == 400


def test_open_file_launches_the_associated_viewer(monkeypatch):
    seen = []
    monkeypatch.setattr(control.os, "startfile", lambda path: seen.append(path))
    control.open_file(r"D:\Docs\report.pdf")
    assert seen == [r"D:\Docs\report.pdf"]


def test_open_file_propagates_the_error(monkeypatch):
    def boom(path):
        raise OSError("no application is associated with this file type")

    monkeypatch.setattr(control.os, "startfile", boom)
    with pytest.raises(OSError, match="associated"):
        control.open_file(r"D:\Docs\mystery.xyz")


def test_open_api_needs_the_token(web, monkeypatch):
    monkeypatch.setattr(control, "open_file", lambda path: None)
    assert call(web, "/api/open", {"path": "D:\\Docs\\report.pdf"})[0] == 403
    assert call(web, "/api/open", {"path": "D:\\Docs\\report.pdf"}, "secret")[0] == 200


def test_open_api_reports_a_failure_instead_of_a_generic_error(web, monkeypatch):
    monkeypatch.setattr(control, "open_file",
                        lambda path: (_ for _ in ()).throw(OSError("no desktop in this session")))
    status, body = call(web, "/api/open", {"path": "D:\\Docs\\report.pdf"}, "secret")
    assert status == 400
    assert "no desktop in this session" in json.loads(body)["error"]


def test_retry_rebuild_and_maintenance_need_the_token_and_queue_requests(web):
    for path, body in (("/api/retry", {"status": "too_big"}), ("/api/rebuild", {}),
                       ("/api/maintenance", {"action": "vacuum"})):
        assert call(web, path, body)[0] == 403
    assert not (web.data_dir / "requests").exists()

    assert call(web, "/api/retry", {"status": "too_big"}, "secret")[0] == 200
    assert call(web, "/api/rebuild", {}, "secret")[0] == 200
    assert call(web, "/api/maintenance", {"action": "vacuum"}, "secret")[0] == 200

    queued = [json.loads(p.read_text(encoding="utf-8")) for p in (web.data_dir / "requests").glob("*.json")]
    assert sorted(queued, key=str) == sorted([{"status": "too_big"}, {"rebuild": True}, {"maintenance": "vacuum"}],
                                             key=str)


@pytest.mark.parametrize("path,body", [
    ("/api/retry", {"status": "ok"}), ("/api/retry", {}), ("/api/maintenance", {"action": "rm -rf"}),
    ("/api/open", {}), ("/api/open", {"path": "   "}),
])
def test_bad_actions_are_refused(web, path, body):
    assert call(web, path, body, "secret")[0] == 404
    assert not (web.data_dir / "requests").exists()


def test_open_folder_shows_the_file_selected_in_explorer(tmp_path, monkeypatch):
    f = tmp_path / "My Docs" / "report.pdf"
    f.parent.mkdir()
    f.write_bytes(b"x")
    seen = []
    monkeypatch.setattr(control.subprocess, "run", lambda args, **kw: seen.append((args, kw)))

    control.open_folder(str(f))

    # a command line string, not a list: Explorer understands /select,"path" but not "/select,path" (it then
    # opens Documents), and Python quotes a list item that holds spaces as a whole
    assert seen[0][0] == f'explorer.exe /select,"{f}"'

    assert seen[0][1]["check"] is False  # Explorer reports 1 even when it worked


def test_open_folder_of_a_file_that_is_gone_opens_the_folder_it_was_in(tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(control.os, "startfile", lambda path: opened.append(path))
    monkeypatch.setattr(control.subprocess, "run", lambda *a, **kw: pytest.fail("nothing to select"))

    control.open_folder(str(tmp_path / "deleted.pdf"))

    assert opened == [str(tmp_path)]


def test_open_folder_of_a_folder_that_is_gone_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="no longer exists"):
        control.open_folder(str(tmp_path / "gone" / "a.pdf"))


def test_open_folder_refuses_a_relative_path():
    with pytest.raises(ValueError, match="full path"):
        control.open_folder("report.pdf")


def test_open_api_can_open_the_folder_instead(web, monkeypatch):
    opened = []
    monkeypatch.setattr(control, "open_file", lambda path: opened.append(("file", path)))
    monkeypatch.setattr(control, "open_folder", lambda path: opened.append(("folder", path)))
    path = "D:\\Docs\\report.pdf"

    assert call(web, "/api/open", {"path": path, "target": "folder"})[0] == 403  # the token, as for the file
    assert call(web, "/api/open", {"path": path, "target": "folder"}, "secret")[0] == 200
    assert call(web, "/api/open", {"path": path, "target": "file"}, "secret")[0] == 200
    assert call(web, "/api/open", {"path": path}, "secret")[0] == 200  # the older request: the file
    assert opened == [("folder", path), ("file", path), ("file", path)]
    assert call(web, "/api/open", {"path": path, "target": "desktop"}, "secret")[0] == 404


def test_open_api_reports_why_the_folder_could_not_be_opened(web, monkeypatch):
    monkeypatch.setattr(control, "open_folder",
                        lambda path: (_ for _ in ()).throw(FileNotFoundError("the folder no longer exists")))
    status, body = call(web, "/api/open", {"path": "D:\\Gone\\a.pdf", "target": "folder"}, "secret")

    assert status == 400 and "no longer exists" in json.loads(body)["error"]
