"""Controlling a search: the hit limit, cancelling, the per-tab "latest search wins" rule, the deadline."""

import json
import socket
import sqlite3
import threading
import time

import pytest

from sindex import config, db
from sindex.indexer import Indexer
from sindex.search import search
from sindex.web import server as server_module
from sindex.web.server import FoundCache, SearchRegistry, SearchTicket, WebServer

from .conftest import call

ENDLESS = "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n) SELECT count(*) FROM n"


@pytest.fixture
def many(cfg, tmp_path):
    """40 files that all contain the word "common", and one whose *name* contains it."""
    root = tmp_path / "docs"
    root.mkdir()
    for i in range(40):
        (root / f"note{i}.txt").write_text(f"common word number {i}", encoding="utf-8")
    (root / "common-name.bin.txt").write_text("nothing to see", encoding="utf-8")
    assert Indexer(cfg, [str(root)], until_idle=True, limits=False).run() == 0
    return cfg


def test_hit_limit_caps_the_ranked_hits(many, monkeypatch):
    monkeypatch.setattr("sindex.search.HIT_LIMIT", 5)
    conn = db.connect(many.db_path, readonly=True)
    try:
        small = search(conn, "common")
    finally:
        conn.close()
    monkeypatch.undo()
    conn = db.connect(many.db_path, readonly=True)
    try:
        full = search(conn, "common")
    finally:
        conn.close()

    assert small["capped"] and small["total"] == 5
    assert not full["capped"] and full["total"] == 40


def test_hit_limit_caps_the_name_scan_too(many):
    conn = db.connect(many.db_path, readonly=True)
    try:
        res = search(conn, "common", names=True)
    finally:
        conn.close()

    assert res["total"] == 41  # 40 by content, 1 more by name


def test_a_newer_search_of_the_tab_cancels_the_older_one():
    registry = SearchRegistry()

    first = registry.begin("tab", 1)
    second = registry.begin("tab", 2)

    assert first.cancelled and not second.cancelled


def test_other_tabs_do_not_cancel_each_other():
    registry = SearchRegistry()

    a = registry.begin("a", 1)
    b = registry.begin("b", 2)

    assert not a.cancelled and not b.cancelled


def test_a_search_that_arrives_after_a_newer_one_is_cancelled_at_once():
    registry = SearchRegistry()
    newer = registry.begin("tab", 5)

    late = registry.begin("tab", 3)

    assert late.cancelled and not newer.cancelled


def test_a_search_without_a_tab_is_never_cancelled():
    registry = SearchRegistry()

    first = registry.begin(None, 1)
    second = registry.begin(None, 2)

    assert not first.cancelled and not second.cancelled


def test_a_finished_search_leaves_no_trace():
    registry = SearchRegistry()
    first = registry.begin("tab", 1)
    registry.end("tab", first)

    second = registry.begin("tab", 2)

    assert not first.cancelled and not second.cancelled
    registry.end("tab", second)
    assert registry._current == {}


def test_cancel_stops_the_running_search_of_that_tab_only():
    registry = SearchRegistry()
    mine = registry.begin("mine", 4)
    other = registry.begin("other", 4)

    registry.cancel("mine", 4)

    assert mine.cancelled and not other.cancelled


def test_cancel_leaves_a_newer_search_alone():
    # the page cancels search 3 when the user edits the box, then starts search 5; the cancel may arrive late
    registry = SearchRegistry()
    newer = registry.begin("tab", 5)

    registry.cancel("tab", 3)
    registry.cancel("unknown-tab", 9)  # nothing is running there: not an error

    assert not newer.cancelled


def test_the_progress_handler_stops_a_running_statement_when_cancelled():
    ticket = SearchTicket(1, timeout_s=60)
    conn = sqlite3.connect(":memory:")
    conn.set_progress_handler(ticket.should_stop, 100)
    ticket.cancelled = True

    with pytest.raises(sqlite3.OperationalError, match="interrupted"):
        conn.execute(ENDLESS).fetchone()

    assert ticket.stopped and not ticket.timed_out


def test_the_progress_handler_stops_a_statement_that_runs_too_long():
    ticket = SearchTicket(1, timeout_s=0.05)
    conn = sqlite3.connect(":memory:")
    conn.set_progress_handler(ticket.should_stop, 100)
    started = time.monotonic()

    with pytest.raises(sqlite3.OperationalError, match="interrupted"):
        conn.execute(ENDLESS).fetchone()

    assert ticket.timed_out and not ticket.cancelled
    assert time.monotonic() - started < 5


def test_the_api_answers_a_superseded_search_as_cancelled(cfg, many):
    cfg.web_port = _free_port()
    server = WebServer(cfg)
    server.start()
    try:
        server._server.searches.begin("tab1", 9)  # a newer search of the same tab is already running
        status, body = call(cfg, "/api/search?query=common&tab=tab1&seq=3")
    finally:
        server.stop()

    assert status == 200
    data = json.loads(body)
    assert data["cancelled"] is True and data["results"] == []


def test_the_cancel_endpoint_stops_the_search_the_page_gave_up_on(cfg, many):
    cfg.web_port = _free_port()
    server = WebServer(cfg)
    server.start()
    try:
        running = server._server.searches.begin("tab1", 7)
        newer = server._server.searches.begin("tab2", 9)
        assert call(cfg, "/api/search/cancel?tab=tab1&seq=7")[0] == 200
        call(cfg, "/api/search/cancel?tab=tab2&seq=8")  # an older search than the running one
        assert call(cfg, "/api/search/cancel?tab=../x&seq=1")[0] == 200  # a bad id is ignored
        assert call(cfg, "/api/search/cancel?tab=tab1&seq=x")[0] == 400
    finally:
        server.stop()

    assert running.cancelled and not newer.cancelled


def test_the_api_ignores_a_malformed_tab_id(web, many):
    status, body = call(web, "/api/search?query=common&tab=../../x&seq=1")

    assert status == 200 and json.loads(body)["total"] == 40


def test_a_bad_sequence_number_is_an_error_message_not_a_crash(web, many):
    status, body = call(web, "/api/search?query=common&tab=abc&seq=x")

    assert status == 200 and json.loads(body)["error"]


def test_a_slow_search_does_not_block_the_status(web, many):
    # a search that is held in its slot must not stop /api/status: the server answers requests concurrently
    from sindex.web import server as web_server

    held = threading.Event()
    release = threading.Event()
    real = web_server.find

    def slow(*args, **kwargs):
        held.set()
        assert release.wait(10), "the test never released the search"
        return real(*args, **kwargs)

    web_server.find = slow
    try:
        t = threading.Thread(target=call, args=(web, "/api/search?query=common"), daemon=True)
        t.start()
        assert held.wait(10)
        status, _ = call(web, "/api/status")
    finally:
        release.set()
        web_server.find = real
        t.join(10)

    assert status == 200


def test_complete_after_defaults_to_one_second_and_validates():
    assert config.Config().search_complete_after_s == 1.0
    assert config.validate_changes({"search_complete_after_s": 0}) == {"search_complete_after_s": 0.0}
    assert config.validate_changes({"search_complete_after_s": 2.5}) == {"search_complete_after_s": 2.5}
    for bad in (0.1, -1, 61, "soon"):
        with pytest.raises(config.ConfigError):
            config.validate_changes({"search_complete_after_s": bad})


def test_complete_after_survives_a_save_and_load(cfg):
    cfg.search_complete_after_s = 3.5
    config.save(cfg)

    assert config.load(cfg.data_dir).search_complete_after_s == 3.5


def test_the_search_page_carries_the_saved_timeout(web):
    _, body = call(web, "/")
    assert "const COMPLETE_AFTER = 1.0;" in body.decode("utf-8")

    config.apply(web.data_dir, {"search_complete_after_s": 2.5})
    _, body = call(web, "/")
    assert "const COMPLETE_AFTER = 2.5;" in body.decode("utf-8")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_the_found_cache_keeps_one_search_per_tab_for_the_same_words():
    cache = FoundCache(ttl_s=60, tabs=2)
    found = object()

    cache.put("t1", ("common",), found)  # type: ignore[arg-type]

    assert cache.get("t1", ("common",)) is found
    assert cache.get("t1", ("rare",)) is None  # another search of the tab
    assert cache.get("t2", ("common",)) is None  # another tab


def test_the_found_cache_forgets_old_entries_and_old_tabs():
    expired = FoundCache(ttl_s=-1, tabs=2)
    expired.put("t1", ("a",), object())  # type: ignore[arg-type]
    assert expired.get("t1", ("a",)) is None

    cache = FoundCache(ttl_s=60, tabs=2)
    for tab in ("t1", "t2", "t3"):
        cache.put(tab, ("a",), object())  # type: ignore[arg-type]
    assert cache.get("t1", ("a",)) is None  # the oldest tab made room
    assert cache.get("t3", ("a",)) is not None


def test_another_sort_or_page_does_not_search_the_text_again(cfg, many, monkeypatch):
    cfg.web_port = _free_port()
    calls = []
    real = server_module.find
    monkeypatch.setattr(server_module, "find", lambda *a, **kw: (calls.append(a[1]), real(*a, **kw))[1])
    server = WebServer(cfg)
    server.start()

    def search_with(qs):
        status, body = call(cfg, "/api/search?tab=t1&" + qs)
        assert status == 200
        return json.loads(body)

    try:
        first = search_with("query=common&seq=1")
        assert len(calls) == 1 and first["total"] == 40

        by_name = search_with("query=common&seq=2&sort=name_desc&reuse=1")
        page_two = search_with("query=common&seq=3&sort=name_desc&reuse=1&offset=20")
        assert len(calls) == 1, "the sort and the page used the kept search"
        names_ = [r["name"] for r in by_name["results"]] + [r["name"] for r in page_two["results"]]
        assert names_ == sorted(names_, key=str.casefold, reverse=True) and len(names_) == 40

        search_with("query=common&seq=4&sort=name_desc")  # no reuse: the person asked again
        assert len(calls) == 2
        search_with("query=rare&seq=5&reuse=1")  # other words: nothing to reuse
        assert len(calls) == 3
        search_with("query=common&seq=6&names=1&reuse=1")  # another filter: nothing to reuse
        assert len(calls) == 4
    finally:
        server.stop()


def test_a_kept_search_ends_after_its_time(cfg, many, monkeypatch):
    cfg.web_port = _free_port()
    calls = []
    real = server_module.find
    monkeypatch.setattr(server_module, "find", lambda *a, **kw: (calls.append(1), real(*a, **kw))[1])
    server = WebServer(cfg)
    server.start()
    server._server.found = FoundCache(ttl_s=-1)  # every entry is already too old
    try:
        call(cfg, "/api/search?query=common&tab=t1&seq=1")
        call(cfg, "/api/search?query=common&tab=t1&seq=2&sort=size_asc&reuse=1")
    finally:
        server.stop()

    assert len(calls) == 2, "an expired search is searched again"


def test_a_failed_search_is_not_kept(cfg, many, monkeypatch):
    cfg.web_port = _free_port()
    server = WebServer(cfg)
    server.start()
    try:
        _, body = call(cfg, "/api/search?query=%28&regex=1&tab=t1&seq=1")
        assert "Invalid regular expression" in json.loads(body)["error"]
        assert server._server.found.get("t1", ()) is None
        assert server._server.found._entries == {}
    finally:
        server.stop()
