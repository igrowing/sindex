"""The three independent boxes: also in names, Use RegEx, Case sensitive."""

import json
import threading
import time
from urllib.parse import quote

import pytest

from sindex import cli, db
from sindex.search import MAX_REGEX_CHARS, search

from .conftest import call
from .test_index_search import named, names, run_index  # noqa: F401  (the fixture is used by name)


def _error(cfg, query, **kw) -> str:
    conn = db.connect(cfg.db_path, readonly=True)
    try:
        return search(conn, query, **kw)["error"]
    finally:
        conn.close()


def test_case_sensitive_names_match_the_exact_letters(named):  # noqa: F811
    assert names(named, "Invoices2024", names=True, case_sensitive=True) == {"scan.txt", "notes.txt"}
    assert names(named, "invoices2024", names=True, case_sensitive=True) == set()
    assert names(named, "invoices2024", names=True) == {"scan.txt", "notes.txt"}  # off: case is ignored


def test_case_sensitive_names_also_see_accents(named):  # noqa: F811
    assert names(named, "café", names=True, case_sensitive=True) == {"café menu.txt"}
    assert names(named, "cafe", names=True, case_sensitive=True) == set()


def test_case_sensitive_keeps_the_query_syntax(named):  # noqa: F811
    assert names(named, "Invoices2024 -scan", names=True, case_sensitive=True) == {"notes.txt"}
    assert names(named, "Invoices2024 -Invoices2024", names=True, case_sensitive=True) == set()


def test_case_sensitive_applies_to_the_contents_too(named):  # noqa: F811
    assert names(named, "zebra", case_sensitive=True) == {"notes.txt"}
    assert names(named, "ZEBRA", case_sensitive=True) == set()


def test_case_sensitive_does_not_add_names_by_itself(named):  # noqa: F811
    assert names(named, "Invoices2024", case_sensitive=True) == set()  # the names box is off


def test_regex_matches_the_full_path_without_regard_to_case(named):  # noqa: F811
    assert names(named, r"\.txt$", names=True, regex=True) >= {"scan.txt", "notes.txt", "zebra-plan.txt"}
    assert names(named, r"invoices\d{4}.scan", names=True, regex=True) == {"scan.txt"}  # folder + file
    assert names(named, r"^zebra", names=True, regex=True) == set()  # the path starts with the drive
    assert names(named, r"\\zebra-", names=True, regex=True) == {"zebra-plan.txt"}


def test_regex_can_be_case_sensitive(named):  # noqa: F811
    assert names(named, "invoices2024", names=True, regex=True) == {"scan.txt", "notes.txt"}
    assert names(named, "invoices2024", names=True, regex=True, case_sensitive=True) == set()
    assert names(named, "Invoices[0-9]+", names=True, regex=True, case_sensitive=True) == {"scan.txt", "notes.txt"}


def test_regex_searches_the_contents_and_with_the_names_box_the_paths(named):  # noqa: F811
    assert names(named, "zebra", regex=True) == {"notes.txt"}  # the word is in its text
    assert names(named, "zebra", names=True, regex=True) == {"notes.txt", "zebra-plan.txt"}  # and in a name


def test_regex_works_with_non_latin_names(named):  # noqa: F811
    assert names(named, "отчёт", names=True, regex=True) == {"Мой отчёт.txt"}
    assert names(named, "ОТЧЁТ", names=True, regex=True, case_sensitive=True) == set()


def test_regex_finds_names_only_with_the_names_box(named):  # noqa: F811
    assert names(named, "zebra-", regex=True) == set()
    assert names(named, "zebra-", regex=True, names=True) == {"zebra-plan.txt"}
    assert names(named, "invoices.*scan", regex=True, names=True, case_sensitive=True) == set()
    assert names(named, "Invoices.*scan", regex=True, names=True, case_sensitive=True) == {"scan.txt"}


def test_regex_combines_with_the_other_filters(named, tmp_path):  # noqa: F811
    assert names(named, r"\.txt$", names=True, regex=True, exts=["txt"], path=str(tmp_path / "docs" / "Invoices2024")) \
        == {"scan.txt", "notes.txt"}


@pytest.mark.parametrize("pattern", ["(", "[a-", "*abc", "(?P<x"])
def test_a_bad_regex_is_an_error_message(named, pattern):  # noqa: F811
    assert _error(named, pattern, names=True, regex=True).startswith("Invalid regular expression")


def test_an_empty_or_huge_regex_is_refused(named):  # noqa: F811
    assert "Type a regular expression" in _error(named, "  ", names=True, regex=True)
    assert "longer than" in _error(named, "a" * (MAX_REGEX_CHARS + 1), names=True, regex=True)


def test_the_api_takes_regex_and_case(web, named):  # noqa: F811
    def total(qs):
        status, body = call(web, "/api/search?" + qs)
        assert status == 200
        return json.loads(body)

    assert total("query=Invoices%5Cd%2B&names=1&regex=1")["total"] == 2
    assert total("query=Invoices%5Cd%2B&names=1&regex=1&case=1")["total"] == 2
    assert total("query=invoices%5Cd%2B&names=1&regex=1&case=1")["total"] == 0
    assert total("query=invoices2024&names=1&case=1")["total"] == 0
    assert total("query=(&names=1&regex=1")["error"].startswith("Invalid regular expression")


def test_the_three_boxes_are_independent_in_the_api(web, named):  # noqa: F811
    def found(qs):
        data = json.loads(call(web, "/api/search?" + qs)[1])
        return {r["name"] for r in data["results"]}

    assert found("query=zebra") == {"notes.txt"}  # none ticked: contents
    assert found("query=zebra&names=1") == {"notes.txt", "zebra-plan.txt"}
    assert found("query=zebra&regex=1") == {"notes.txt"}  # a regex over the contents
    assert found("query=zebra&regex=1&names=1") == {"notes.txt", "zebra-plan.txt"}
    assert found("query=ZEBRA&case=1") == set()  # exact letters, in the contents too
    assert found("query=ZEBRA&names=1&case=1") == set()
    assert found("query=zebra&names=1&case=1") == {"notes.txt", "zebra-plan.txt"}
    assert found("query=ZEBRA&regex=1&case=1") == set()
    assert found("query=ZEBRA&regex=1") == {"notes.txt"}


def test_the_cli_searches_by_regex(named, capsys):  # noqa: F811
    code = cli.main(["--data-dir", str(named.data_dir), "search", "--names", "--regex", "--case", r"Invoices\d+"])

    assert code == 0
    out = capsys.readouterr().out
    assert "scan.txt" in out and "notes.txt" in out


RUNAWAY = r"(a|aa)+$"  # exponential on a long run of "a" followed by anything else


@pytest.fixture
def runaway(cfg, tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    (root / ("a" * 60 + "b.txt")).write_text("x", encoding="utf-8")
    (root / "fine.txt").write_text("x", encoding="utf-8")
    run_index(cfg, root)
    return cfg


def test_a_runaway_regex_is_stopped_with_a_message(runaway):
    started = time.monotonic()

    error = _error(runaway, RUNAWAY, names=True, regex=True)

    assert "took longer than" in error and "aaaa" in error
    assert time.monotonic() - started < 10


def test_a_runaway_regex_does_not_freeze_the_other_threads(runaway):
    # the stdlib re kept the GIL for the whole match, so no other thread ran; here this one must keep ticking
    done = threading.Event()
    threading.Thread(target=lambda: (_error(runaway, RUNAWAY, names=True, regex=True), done.set()),
                     daemon=True).start()  # daemon: the test fails rather than hangs if it never ends
    ticks = 0
    while not done.wait(0.05):
        ticks += 1
        assert ticks < 400, "the search never ended"

    assert ticks >= 5


def test_the_api_survives_a_runaway_regex(web, runaway):  # noqa: F811
    status, body = call(web, "/api/search?query=" + quote(RUNAWAY) + "&names=1&regex=1")

    assert status == 200 and "took longer than" in json.loads(body)["error"]
    assert call(web, "/api/status")[0] == 200


def test_a_regex_that_is_merely_big_still_works(runaway):
    assert names(runaway, r"^.*\\a{10,}b\.txt$", names=True, regex=True) == {"a" * 60 + "b.txt"}


def test_a_runaway_regex_over_the_contents_is_stopped_too(cfg, tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "long.txt").write_text("a" * 60 + "b", encoding="utf-8")
    run_index(cfg, root)
    started = time.monotonic()

    error = _error(cfg, RUNAWAY, regex=True)

    assert "took longer than" in error and "some text" in error
    assert time.monotonic() - started < 10
