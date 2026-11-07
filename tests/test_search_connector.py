"""The Explorer search connector: /opensearch.xml (the description) and /api/search.rss (the results)."""

from urllib.parse import quote

import regex

from sindex.indexer import Indexer

from .conftest import call


def _index(cfg, tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "invoice.txt").write_text("an overdue invoice for the roof repair", encoding="utf-8")
    (root / "other.txt").write_text("nothing relevant here", encoding="utf-8")
    assert Indexer(cfg, [str(root)], until_idle=True, limits=False).run() == 0
    return root


def test_opensearch_description_points_at_the_rss_endpoint(web):
    status, body = call(web, "/opensearch.xml")

    assert status == 200
    text = body.decode("utf-8")
    assert "<ShortName>sindex</ShortName>" in text
    assert f"http://127.0.0.1:{web.web_port}/api/search.rss?q={{searchTerms}}" in text


def test_search_rss_lists_matching_files_as_file_links(web, tmp_path):
    root = _index(web, tmp_path)

    status, body = call(web, "/api/search.rss?q=invoice")

    assert status == 200
    text = body.decode("utf-8")
    assert "<title>invoice.txt</title>" in text
    expected_link = "file:///" + quote(str(root / "invoice.txt").replace("\\", "/"), safe="/:")
    assert f"<link>{expected_link}</link>" in text
    assert "other.txt" not in text
    assert "<opensearch:totalResults>1</opensearch:totalResults>" in text


def test_search_rss_with_no_match_is_an_empty_but_valid_feed(web, tmp_path):
    _index(web, tmp_path)

    status, body = call(web, "/api/search.rss?q=nonexistentword")

    assert status == 200
    text = body.decode("utf-8")
    assert "<opensearch:totalResults>0</opensearch:totalResults>" in text
    assert not regex.search(r"<item>", text)


def test_search_rss_before_the_index_exists_is_an_empty_feed(web):
    status, body = call(web, "/api/search.rss?q=anything")

    assert status == 200
    assert "<opensearch:totalResults>0</opensearch:totalResults>" in body.decode("utf-8")


def test_search_rss_rejects_a_bad_count_or_start_index(web, tmp_path):
    _index(web, tmp_path)

    status, _ = call(web, "/api/search.rss?q=invoice&count=nope")
    assert status == 400

    status, _ = call(web, "/api/search.rss?q=invoice&startIndex=nope")
    assert status == 400


def test_search_rss_escapes_special_characters_in_the_snippet(web, tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "notes.txt").write_text("a <script>alert('x')</script> & more about tags", encoding="utf-8")
    assert Indexer(web, [str(root)], until_idle=True, limits=False).run() == 0

    status, body = call(web, "/api/search.rss?q=script")

    assert status == 200
    text = body.decode("utf-8")
    assert "<script>" not in text  # would break the feed XML if left unescaped
    assert "&lt;script&gt;" in text or "&amp;lt;script&amp;gt;" in text
