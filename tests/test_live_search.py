"""Search while typing: the hit limit, the per-tab "latest search wins" rule, the deadline, the setting."""

import json
import socket
import sqlite3
import threading
import time

import pytest

from sindex import config, db
from sindex.indexer import Indexer
from sindex.search import HIT_LIMIT, LIVE_HIT_LIMIT, search
from sindex.web.server import SearchRegistry, SearchTicket, WebServer

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


def test_the_live_limit_is_a_tenth_of_the_full_one():
    assert LIVE_HIT_LIMIT == 200 and HIT_LIMIT == 2000


def test_hit_limit_caps_the_ranked_hits(many):
    conn = db.connect(many.db_path, readonly=True)
    try:
        small = search(conn, "common", hit_limit=5)
        full = search(conn, "common")
    finally:
        conn.close()

    assert small["capped"] and small["total"] == 5
    assert not full["capped"] and full["total"] == 40


def test_hit_limit_caps_the_name_scan_too(many):
    conn = db.connect(many.db_path, readonly=True)
    try:
        res = search(conn, "common", names=True, hit_limit=1000)
    finally:
        conn.close()

    assert res["total"] == 41  # 40 by content, 1 more by name


def test_quick_search_skips_the_name_scan_and_says_so(web, many):
    _, body = call(web, "/api/search?q=common&names=1&quick=1")
    quick = json.loads(body)
    _, body = call(web, "/api/search?q=common&names=1")
    full = json.loads(body)

    assert quick["quick"] is True and quick["total"] == 40
    assert full["quick"] is False and full["total"] == 41


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
        status, body = call(cfg, "/api/search?q=common&tab=tab1&seq=3")
    finally:
        server.stop()

    assert status == 200
    data = json.loads(body)
    assert data["cancelled"] is True and data["results"] == []


def test_the_api_ignores_a_malformed_tab_id(web, many):
    status, body = call(web, "/api/search?q=common&tab=../../x&seq=1")

    assert status == 200 and json.loads(body)["total"] == 40


def test_a_bad_sequence_number_is_an_error_message_not_a_crash(web, many):
    status, body = call(web, "/api/search?q=common&tab=abc&seq=x")

    assert status == 200 and json.loads(body)["error"]


def test_a_slow_search_does_not_block_the_status(web, many):
    # a search that is held in its slot must not stop /api/status: the server answers requests concurrently
    from sindex.web import server as web_server

    held = threading.Event()
    release = threading.Event()
    real = web_server.search

    def slow(*args, **kwargs):
        held.set()
        assert release.wait(10), "the test never released the search"
        return real(*args, **kwargs)

    web_server.search = slow
    try:
        t = threading.Thread(target=call, args=(web, "/api/search?q=common"), daemon=True)
        t.start()
        assert held.wait(10)
        status, _ = call(web, "/api/status")
    finally:
        release.set()
        web_server.search = real
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
