"""The "Export to CSV" button: every file of the search on the page, as CSV, from /api/export.csv."""

import csv
import io
import json
import os
import urllib.request
from datetime import date, datetime
from urllib.parse import quote, unquote

import pytest

from sindex import db
from sindex.search import find, present
from sindex.web import server as server_module
from sindex.web.server import EXPORT_COLUMNS, export_filename, results_csv

from .conftest import call
from .test_index_search import run_index

COUNT = 25  # more than a page of 20


@pytest.fixture
def many(cfg, tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    for i in range(COUNT):
        name = f"f{i:02d}.txt" if i % 5 else f"f{i:02d}.md"
        f = root / name
        f.write_text(f"intro, \"quoted\" and <b>tagged</b> export marker {i} outro" + "x" * i, encoding="utf-8")
        os.utime(f, (1_600_000_000 + i * 100, 1_600_000_000 + i * 100))
    run_index(cfg, root)
    return cfg


def rows(body: bytes) -> list[list[str]]:
    return list(csv.reader(io.StringIO(body.decode("utf-8-sig"))))


# ---- the file format ------------------------------------------------------------------------------------------

def test_a_row_has_folder_name_size_date_and_the_text_without_highlighting():
    csv_text = results_csv([{
        "dir": "D:\\docs", "name": "a, b.txt", "size": 1234, "mtime": 1_600_000_000.0,
        "snippet": "before <mark>word</mark> &amp; &lt;b&gt; after",
    }])

    header, row = list(csv.reader(io.StringIO(csv_text)))
    assert tuple(header) == EXPORT_COLUMNS == ("path", "filename", "size_bytes", "modified", "content")
    assert row[:3] == ["D:\\docs", "a, b.txt", "1234"]  # a comma in a name stays in its cell
    assert row[3] == datetime.fromtimestamp(1_600_000_000).strftime("%Y-%m-%d %H:%M:%S")
    assert row[4] == "before word & <b> after"


def test_quotes_and_line_breaks_are_escaped_and_lines_end_with_crlf():
    csv_text = results_csv([{"dir": "D:\\d", "name": "n.txt", "size": 1, "mtime": 0.0,
                             "snippet": 'she said "hi",\n then left'}])

    assert csv_text.endswith("\r\n")
    assert list(csv.reader(io.StringIO(csv_text)))[1][4] == 'she said "hi", then left'


def test_a_file_found_by_name_has_an_empty_content_cell():
    row = list(csv.reader(io.StringIO(results_csv(
        [{"dir": "D:\\d", "name": "n.txt", "size": 1, "mtime": 0.0, "snippet": ""}]))))[1]

    assert row[4] == ""


@pytest.mark.parametrize("start", ["=", "+", "-", "@", "\t"])
def test_text_that_a_spreadsheet_would_run_as_a_formula_is_quoted(start):
    row = list(csv.reader(io.StringIO(results_csv(
        [{"dir": "D:\\d", "name": start + "name.txt", "size": 1, "mtime": 0.0, "snippet": start + "cmd"}]))))[1]

    assert row[1] == "'" + start + "name.txt"
    if start != "	":  # the text has its white space tidied first, so a leading tab is gone before the check
        assert row[4] == "'" + start + "cmd"


def test_a_date_before_1970_is_given_in_utc_and_says_so():
    row = list(csv.reader(io.StringIO(results_csv(
        [{"dir": "D:\\d", "name": "n.txt", "size": 1, "mtime": -86400.0, "snippet": ""}]))))[1]

    assert row[3] == "1969-12-31T00:00:00Z"


def test_non_latin_text_survives():
    text = "日本語 Ελληνικά שלום 😀"
    row = list(csv.reader(io.StringIO(results_csv(
        [{"dir": "D:\\d", "name": "n.txt", "size": 1, "mtime": 0.0, "snippet": text}]))))[1]

    assert row[4] == text


# ---- present(): all files instead of a page ----------------------------------------------------------------------

def test_present_without_a_limit_returns_every_file(many):
    conn = db.connect(many.db_path, readonly=True)
    try:
        found = find(conn, "marker")
        assert len(present(conn, found, limit=20)["results"]) == 20
        assert len(present(conn, found, limit=None)["results"]) == COUNT
        assert len(present(conn, found, limit=None, offset=5)["results"]) == COUNT - 5
    finally:
        conn.close()


# ---- the endpoint -----------------------------------------------------------------------------------------------

def test_the_export_has_all_pages_not_only_the_one_shown(many, web):
    status, body = call(web, "/api/export.csv?q=marker&offset=20")

    assert status == 200
    assert body.startswith(b"\xef\xbb\xbf")  # a byte order mark: spreadsheets read UTF-8 correctly
    table = rows(body)
    assert tuple(table[0]) == EXPORT_COLUMNS
    assert len(table) - 1 == COUNT
    assert {r[1] for r in table[1:]} == {f"f{i:02d}.{'txt' if i % 5 else 'md'}" for i in range(COUNT)}


def test_the_row_matches_what_the_page_shows(many, web):
    table = rows(call(web, "/api/export.csv?q=marker&sort=name_asc")[1])
    first = table[1]

    assert first[0] == str(many.data_dir.parent / "docs") and first[1] == "f00.md"
    assert int(first[2]) == os.path.getsize(os.path.join(first[0], first[1]))
    assert first[3] == datetime.fromtimestamp(1_600_000_000).strftime("%Y-%m-%d %H:%M:%S")
    assert "marker 0" in first[4] and "<mark>" not in first[4] and "&amp;" not in first[4]
    assert '"quoted"' in first[4] and "<b>tagged</b>" in first[4]


def test_the_export_keeps_the_sort_order_and_the_extension_filters(many, web):
    by_size = rows(call(web, "/api/export.csv?q=marker&sort=size_desc")[1])[1:]
    assert [int(r[2]) for r in by_size] == sorted((int(r[2]) for r in by_size), reverse=True)

    only_md = rows(call(web, "/api/export.csv?q=marker&only_ext=md")[1])[1:]
    assert len(only_md) == 5 and all(r[1].endswith(".md") for r in only_md)

    skip_md = rows(call(web, "/api/export.csv?q=marker&skip_ext=md")[1])[1:]
    assert len(skip_md) == COUNT - 5 and not any(r[1].endswith(".md") for r in skip_md)


def test_the_export_uses_the_kept_search_instead_of_searching_again(many, web, monkeypatch):
    calls = []
    real = server_module.find
    monkeypatch.setattr(server_module, "find", lambda *a, **kw: (calls.append(1), real(*a, **kw))[1])

    assert call(web, "/api/search?tab=t1&seq=1&q=marker")[0] == 200
    status, body = call(web, "/api/export.csv?tab=t1&reuse=1&q=marker&sort=name_desc&only_ext=txt")

    assert status == 200 and len(rows(body)) - 1 == COUNT - 5
    assert len(calls) == 1, "the export filtered the kept search"


def test_an_export_does_not_cancel_the_search_of_its_tab(many, web):
    from sindex.web.server import SearchRegistry

    seen = []
    real = SearchRegistry.begin
    SearchRegistry.begin = lambda self, tab, seq, *a, **kw: (seen.append(tab), real(self, tab, seq, *a, **kw))[1]
    try:
        assert call(web, "/api/export.csv?tab=t1&seq=5&q=marker")[0] == 200
    finally:
        SearchRegistry.begin = real

    assert seen == [None], "an export is not a newer search of the tab"


def test_an_export_of_a_bad_search_is_an_error_not_an_empty_file(many, web):
    status, body = call(web, "/api/export.csv?q=")

    assert status == 400 and json.loads(body)["error"]


def test_nothing_found_exports_only_the_header(many, web):
    status, body = call(web, "/api/export.csv?q=zzznotthere")

    assert status == 200 and [tuple(r) for r in rows(body)] == [EXPORT_COLUMNS]


def test_the_page_has_the_button_next_to_the_extension_menus():
    page = (server_module.STATIC / "index.html").read_text(encoding="utf-8")
    ext_bar = page[page.index('id="extBar"'):page.index('<ol id="results">')]

    assert 'id="exportBtn"' in ext_bar and "Export to CSV" in ext_bar
    assert "/api/export.csv" in page


def test_the_button_starts_greyed_out_and_a_failure_stays_on_the_page():
    page = (server_module.STATIC / "index.html").read_text(encoding="utf-8")
    button = page[page.index('<button type="button" class="plain export"'):]
    button = button[:button.index(">")]

    assert "disabled" in button, "no results yet: nothing to export"
    assert "function syncExport()" in page and "inflight !== null" in page  # greyed out while a search runs
    assert 'id="exportErr"' in page and 'role="alert"' in page  # an error is shown until dismissed, not as a toast
    assert 'toast("Export failed' not in page


def test_the_page_saves_through_a_normal_download_not_a_save_dialog_of_its_own():
    page = (server_module.STATIC / "index.html").read_text(encoding="utf-8")

    assert "function downloadCsv(" in page and "a.download = name" in page
    assert "Content-Disposition" in page  # the server names the file after the search words
    # a browser can refuse a write into the file chosen in showSaveFilePicker, and the person is then asked twice
    assert "showSaveFilePicker" not in page and "createWritable" not in page
    assert "File name for the CSV export" not in page


# ---- the file name: sindex-<what was searched>-<date>.csv, nothing in it that a file name cannot hold ---------

DAY = date(2026, 11, 7)


@pytest.mark.parametrize(("query", "stem"), [
    ("invoice", "invoice"),
    ("invoice overdue", "invoice_overdue"),  # a space is not safe either
    ("kyo*", "kyo"),
    ("k?o", "k_o"),
    ("^start end$", "start_end"),
    ("C:\\Users\\me/docs", "C_Users_me_docs"),
    ('"exact phrase"', "exact_phrase"),
    ("it's <b>|a|</b>", "it_s_b_a_b"),
    ("-kyo OR tokyo", "kyo_OR_tokyo"),
    ("a.b.c", "a_b_c"),  # a dot, too: Windows drops a trailing one
    ("a   b\t\nc", "a_b_c"),  # a run of unsafe characters is one underscore
    ("snake_case-and-hyphen", "snake_case-and-hyphen"),
    ("日本語 Ελληνικά שלום", "日本語_Ελληνικά_שלום"),  # letters of any script stay
    ("  padded  ", "padded"),
])
def test_the_file_name_has_the_search_words_made_safe(query, stem):
    assert export_filename(query, DAY) == f"sindex-{stem}-2026-11-07.csv"


@pytest.mark.parametrize("query", ["", "   ", "***", "?*^\\/'\"", "-_-"])
def test_a_search_without_usable_words_is_called_results(query):
    assert export_filename(query, DAY) == "sindex-results-2026-11-07.csv"


def test_every_forbidden_character_is_gone():
    name = export_filename("".join(chr(c) for c in range(1, 128)), DAY)

    assert not set(name.removesuffix(".csv")) & set("<>:\"/\\|?*^'`~. %$&;()[]{}!#@+=,")  # the dot of .csv stays
    assert name.isprintable() and name.startswith("sindex-") and name.endswith("-2026-11-07.csv")


def test_a_long_search_is_cut_without_a_dangling_separator():
    name = export_filename("word " * 30, DAY)
    stem = name.removeprefix("sindex-").removesuffix("-2026-11-07.csv")

    assert 0 < len(stem) <= 40 and not stem.endswith(("_", "-"))


def disposition(web, query: str) -> str:
    url = f"http://127.0.0.1:{web.web_port}/api/export.csv?q=" + quote(query)
    with urllib.request.urlopen(url, timeout=10) as r:
        assert r.status == 200
        return r.headers["Content-Disposition"]


def test_the_download_carries_the_name(many, web):
    header = disposition(web, "marker")

    assert header.startswith("attachment; filename*=UTF-8''")
    assert unquote(header.split("''", 1)[1]) == export_filename("marker", datetime.now().date())


def test_unsafe_characters_of_the_query_do_not_reach_the_header(many, web):  # noqa: F811
    header = disposition(web, "marker ?*^ /x")

    assert unquote(header.split("''", 1)[1]) == export_filename("marker ?*^ /x", datetime.now().date())
    assert "marker_x" in header


def test_a_name_with_non_latin_letters_travels_in_the_header(many, web):  # noqa: F811
    header = disposition(web, "日本語")

    assert unquote(header.split("''", 1)[1]).startswith("sindex-日本語-")
