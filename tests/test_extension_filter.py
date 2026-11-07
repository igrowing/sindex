"""The "Show only files with extension(s)" and "Exclude files with extension(s)" menus: the extensions of the
found files, and filtering the found files by them."""

import json
import os

import pytest

from sindex import db
from sindex.search import find, present, search

from .conftest import call
from .test_index_search import run_index
from .test_search_control import _free_port  # noqa: F401  (used by the API test)


@pytest.fixture
def mixed(cfg, tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    times = {"a.txt": 1, "b.txt": 2, "c.TXT": 3, "d.md": 4, "e.md": 5, "f.log": 6, "archive.tar.txt": 8}
    for name, mtime in times.items():
        f = root / name
        f.write_text("common words", encoding="utf-8")
        os.utime(f, (1_600_000_000 + mtime, 1_600_000_000 + mtime))
    run_index(cfg, root)
    return cfg


def page(cfg, query="common", find_options=None, **kw):
    conn = db.connect(cfg.db_path, readonly=True)
    try:
        return present(conn, find(conn, query, **(find_options or {})), **kw)
    finally:
        conn.close()


def names(res):
    return sorted(r["name"] for r in res["results"])


def test_the_response_lists_the_extensions_of_all_found_files_with_counts(mixed):
    res = page(mixed, limit=2)  # a page of two files: the list is still about all seven

    assert res["extensions"] == [["txt", 4], ["md", 2], ["log", 1]]  # by count, then name
    assert res["found_total"] == 7 and res["total"] == 7 and len(res["results"]) == 2


def test_a_file_without_an_extension_is_listed_as_an_empty_one(cfg, tmp_path):
    # such a file is only indexed when "no extension" is among the text extensions
    cfg.text_extensions = [*cfg.text_extensions, ""]
    root = tmp_path / "docs"
    root.mkdir()
    (root / "README").write_text("common words", encoding="utf-8")
    (root / "a.txt").write_text("common words", encoding="utf-8")
    run_index(cfg, root)

    res = page(cfg, limit=50)

    assert res["extensions"] == [["", 1], ["txt", 1]]
    assert names(page(cfg, only=frozenset({""}), limit=50)) == ["README"]
    assert names(page(cfg, skip=frozenset({""}), limit=50)) == ["a.txt"]


def test_an_extension_is_the_part_after_the_last_dot_without_case(mixed):
    res = page(mixed, only=frozenset({"txt"}), limit=50)

    assert names(res) == ["a.txt", "archive.tar.txt", "b.txt", "c.TXT"]


def test_only_keeps_the_selected_extensions(mixed):
    assert names(page(mixed, only=frozenset({"md", "log"}), limit=50)) == ["d.md", "e.md", "f.log"]


def test_skip_removes_the_selected_extensions(mixed):
    assert names(page(mixed, skip=frozenset({"txt"}), limit=50)) == ["d.md", "e.md", "f.log"]


def test_the_extension_list_does_not_shrink_when_filtering(mixed):
    # otherwise the person could not select anything else once something is selected
    res = page(mixed, only=frozenset({"md"}), limit=50)

    assert res["total"] == 2 and res["found_total"] == 7
    assert [e for e, _ in res["extensions"]] == ["txt", "md", "log"]


def test_filtering_comes_before_paging_and_after_sorting(mixed):
    first = page(mixed, only=frozenset({"txt"}), sort="date_desc", limit=2)
    second = page(mixed, only=frozenset({"txt"}), sort="date_desc", limit=2, offset=2)

    assert [r["name"] for r in first["results"] + second["results"]] == ["archive.tar.txt", "c.TXT", "b.txt", "a.txt"]
    assert first["total"] == 4


def test_the_filters_also_work_through_search(mixed):
    conn = db.connect(mixed.db_path, readonly=True)
    try:
        res = search(conn, "common", only=frozenset({"md"}), limit=50)
    finally:
        conn.close()

    assert names(res) == ["d.md", "e.md"]


def test_an_error_has_no_extensions(mixed):
    conn = db.connect(mixed.db_path, readonly=True)
    try:
        res = search(conn, "  ")
    finally:
        conn.close()

    assert res["error"] and res.get("extensions", []) == []


def test_the_api_filters_the_kept_search_without_searching_again(cfg, mixed, monkeypatch):
    from sindex.web import server as server_module
    from sindex.web.server import WebServer

    cfg.web_port = _free_port()
    calls = []
    real = server_module.find
    monkeypatch.setattr(server_module, "find", lambda *a, **kw: (calls.append(1), real(*a, **kw))[1])
    server = WebServer(cfg)
    server.start()

    def get(qs):
        status, body = call(cfg, "/api/search?tab=t1&q=common&" + qs)
        assert status == 200
        return json.loads(body)

    try:
        first = get("seq=1")
        only = get("seq=2&reuse=1&only_ext=md,log")
        skip = get("seq=3&reuse=1&skip_ext=txt,.")  # "." stands for "no extension" (none among these)
        both = get("seq=4&reuse=1&only_ext=txt&skip_ext=txt")  # not offered by the page; nothing is left
        assert len(calls) == 1, "the kept search was filtered, not searched again"
    finally:
        server.stop()

    assert first["extensions"][0] == ["txt", 4]
    assert sorted(r["name"] for r in only["results"]) == ["d.md", "e.md", "f.log"] and only["total"] == 3
    assert sorted(r["name"] for r in skip["results"]) == ["d.md", "e.md", "f.log"]
    assert both["total"] == 0
