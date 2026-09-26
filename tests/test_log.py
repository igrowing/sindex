"""The log page: reading the tail of the log files."""

import json

import pytest

from sindex import control

from .conftest import call


@pytest.fixture
def logs(cfg):
    folder = cfg.data_dir / "logs"
    folder.mkdir(exist_ok=True)
    return folder


def test_a_short_log_is_returned_whole(cfg, logs):
    (logs / "indexer.log").write_text("first\nsecond ünïcode\n", encoding="utf-8", newline="\n")

    log = control.read_log(cfg, "indexer")

    assert log == {"name": "indexer", "size": len("first\nsecond ünïcode\n".encode()), "truncated": False,
                   "text": "first\nsecond ünïcode\n"}


def test_only_the_end_of_a_long_log_is_returned_without_a_cut_line(cfg, logs):
    lines = [f"line {i:05d}" for i in range(500)]
    (logs / "indexer.log").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

    log = control.read_log(cfg, "indexer", kb=1)

    assert log["truncated"] and log["size"] == 500 * 11
    shown = log["text"].splitlines()
    assert shown[-1] == "line 00499"
    assert all(len(line) == 10 for line in shown), "the half line at the cut is dropped"
    assert 80 < len(shown) < 94


def test_the_size_asked_for_is_limited(cfg, logs, monkeypatch):
    monkeypatch.setattr(control, "MAX_LOG_KB", 1)
    (logs / "indexer.log").write_text("x\n" * 2000, encoding="utf-8", newline="\n")

    assert len(control.read_log(cfg, "indexer", kb=100000)["text"]) <= 1024
    assert len(control.read_log(cfg, "indexer", kb=0)["text"]) <= 1024  # asking for nothing gives a little


def test_bytes_that_are_not_utf8_do_not_break_reading(cfg, logs):
    (logs / "indexer-stderr.log").write_bytes(b"caf\xe9 broke\n")

    assert "broke" in control.read_log(cfg, "indexer-stderr")["text"]


def test_a_log_that_does_not_exist_yet_is_empty(cfg):
    assert control.read_log(cfg, "service") == {"name": "service", "size": 0, "text": "", "truncated": False}


@pytest.mark.parametrize("name", ["", "../config", "config.toml", "indexer.log", "status"])
def test_only_the_known_logs_can_be_read(cfg, name):
    with pytest.raises(ValueError, match="no such log"):
        control.read_log(cfg, name)


def test_the_log_api_needs_the_token_and_serves_the_log(web, logs):
    (logs / "service.log").write_text("hello log\n", encoding="utf-8", newline="\n")

    assert call(web, "/api/log?name=service")[0] == 403

    status, body = call(web, "/api/log?name=service", token="secret")
    assert status == 200 and json.loads(body)["text"] == "hello log\n"


@pytest.mark.parametrize("query", ["name=..%5Cconfig", "name=nope", "name=service&kb=x"])
def test_the_log_api_refuses_bad_requests(web, query):
    assert call(web, "/api/log?" + query, token="secret")[0] == 400


def test_the_log_page_is_served_and_linked_from_the_search_page(web):
    status, page = call(web, "/log")
    assert status == 200 and b'const TOKEN = "secret"' in page and b"{{" not in page
    assert b'href="/log"' in call(web, "/")[1]
