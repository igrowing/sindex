"""The "Sort by" choices of the result list."""

import os

import pytest

from sindex import db
from sindex.search import SORTS, search

from .test_index_search import run_index


@pytest.fixture
def files(cfg, tmp_path):
    """Four files that all have the word "common", differing in folder, name, size and date."""
    root = tmp_path / "docs"
    spec = {  # path under root: (text, mtime)
        "a/alpha.txt": ("common " + "padding " * 400, 1_600_000_000),  # biggest, oldest
        "a/Beta.txt": ("common beta stuff", 1_600_000_300),
        "b/gamma.txt": ("common " + "pad " * 40, 1_600_000_100),
        "c/delta.txt": ("common", 1_600_000_400),  # smallest, newest
    }
    for rel, (text, mtime) in spec.items():
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
        os.utime(f, (mtime, mtime))
    run_index(cfg, root)
    return cfg


def order(cfg, query="common", **kw) -> list[str]:
    conn = db.connect(cfg.db_path, readonly=True)
    try:
        res = search(conn, query, **kw)
    finally:
        conn.close()
    assert res["error"] is None, res["error"]
    return [r["name"] for r in res["results"]]


@pytest.mark.parametrize(("sort", "expected"), [
    ("path_asc", ["alpha.txt", "Beta.txt", "gamma.txt", "delta.txt"]),  # folder first, then name, case ignored
    ("path_desc", ["delta.txt", "gamma.txt", "Beta.txt", "alpha.txt"]),
    ("name_asc", ["alpha.txt", "Beta.txt", "delta.txt", "gamma.txt"]),
    ("name_desc", ["gamma.txt", "delta.txt", "Beta.txt", "alpha.txt"]),
    ("size_asc", ["delta.txt", "Beta.txt", "gamma.txt", "alpha.txt"]),
    ("size_desc", ["alpha.txt", "gamma.txt", "Beta.txt", "delta.txt"]),
    ("date_asc", ["alpha.txt", "gamma.txt", "Beta.txt", "delta.txt"]),
    ("date_desc", ["delta.txt", "Beta.txt", "gamma.txt", "alpha.txt"]),
])
def test_each_sort_orders_the_whole_result_list(files, sort, expected):
    assert order(files, sort=sort) == expected


def test_sorting_comes_before_paging(files):
    assert order(files, sort="size_asc", limit=2) == ["delta.txt", "Beta.txt"]
    assert order(files, sort="size_asc", limit=2, offset=2) == ["gamma.txt", "alpha.txt"]


def test_relevance_is_the_default_and_the_search_order_stays_for_one_word(files):
    assert order(files, sort="relevance") == order(files)


def test_an_unknown_sort_is_refused(files):
    conn = db.connect(files.db_path, readonly=True)
    try:
        res = search(conn, "common", sort="by-colour")
    finally:
        conn.close()

    assert "sort" in res["error"].lower()
    assert "relevance" in SORTS


def test_sorting_also_orders_name_matches_and_inside_word_hits(cfg, tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "b.txt").write_text("visit tokyo", encoding="utf-8")  # found inside a word, by the scan
    (root / "a.txt").write_text("visit kyo", encoding="utf-8")  # a whole word, by the index
    (root / "kyo_c.txt").write_text("nothing", encoding="utf-8")  # by name only
    run_index(cfg, root)

    # "kyo" by the index, "*kyo" (anywhere in a word) by the scan
    assert order(cfg, "kyo OR *kyo", names=True, sort="name_asc") == ["a.txt", "b.txt", "kyo_c.txt"]
    assert order(cfg, "kyo OR *kyo", names=True, sort="name_desc") == ["kyo_c.txt", "b.txt", "a.txt"]
    assert order(cfg, "kyo OR *kyo", names=True) == ["a.txt", "b.txt", "kyo_c.txt"]  # relevance: index, scan, names


def test_an_exclusion_list_can_be_sorted_too(kyo_like):
    assert order(kyo_like, "-kyo", sort="name_desc") == ["z.txt", "m.txt"]


@pytest.fixture
def kyo_like(cfg, tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    for name, text in {"z.txt": "nothing", "m.txt": "other", "k.txt": "kyoto"}.items():
        (root / name).write_text(text, encoding="utf-8")
    run_index(cfg, root)
    return cfg


@pytest.fixture
def proximity(cfg, tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "near.txt").write_text("alpha beta " + "filler " * 300, encoding="utf-8")  # long: the index prefers short
    (root / "mid.txt").write_text("alpha two three beta", encoding="utf-8")
    (root / "far.txt").write_text("alpha " + "x " * 12 + "beta", encoding="utf-8")
    (root / "alone.txt").write_text("alpha only", encoding="utf-8")  # no beta: not a hit at all
    run_index(cfg, root)
    return cfg


def test_relevance_puts_files_with_the_searched_words_closer_together_first(proximity):
    assert order(proximity, "alpha beta") == ["near.txt", "mid.txt", "far.txt"]


def test_relevance_by_proximity_also_works_for_words_found_inside_others(cfg, tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "far.txt").write_text("xalphax " + "y " * 20 + "ybetay", encoding="utf-8")
    (root / "near.txt").write_text("xalphax ybetay " + "filler " * 300, encoding="utf-8")
    run_index(cfg, root)

    assert order(cfg, "*alpha *beta") == ["near.txt", "far.txt"]


def test_other_sorts_ignore_proximity(proximity):
    assert order(proximity, "alpha beta", sort="name_asc") == ["far.txt", "mid.txt", "near.txt"]


@pytest.fixture
def two_chunks(cfg, tmp_path):
    """One file whose first chunk has the words far apart (many times, so the index ranks it best) and whose second
    chunk has them back to back."""
    cfg.chunk_chars = 300
    root = tmp_path / "docs"
    root.mkdir()
    far = ("alpha " + "filler " * 5 + "beta " + "filler " * 5) * 3
    tail = "plain words here. " * 3 + "alpha beta ends the file"
    (root / "multi.txt").write_text(far + "\n\n" + tail, encoding="utf-8")
    run_index(cfg, root)
    return cfg


def test_the_snippet_shows_the_chunk_where_the_words_are_closest(two_chunks):
    conn = db.connect(two_chunks.db_path, readonly=True)
    try:
        res = search(conn, "alpha beta")
        by_name_sort = search(conn, "alpha beta", sort="name_asc")
    finally:
        conn.close()

    assert "<mark>alpha</mark> <mark>beta</mark>" in res["results"][0]["snippet"]
    # the choice of the closest chunk does not depend on how the files are ordered
    assert "<mark>alpha</mark> <mark>beta</mark>" in by_name_sort["results"][0]["snippet"]


def test_the_snippet_is_centred_on_the_words_that_are_closest_not_on_the_first_hit(cfg, tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    # a punctuated word ("don't") makes the snippet come from the text, not from the index; "worry" comes first
    # in the chunk and is repeated, the pair that belongs together is much later
    text = "worry " * 6 + "filler " * 60 + "and then you don't worry about it"
    (root / "book.txt").write_text(text, encoding="utf-8")
    run_index(cfg, root)
    conn = db.connect(cfg.db_path, readonly=True)
    try:
        snippet = search(conn, "don't worry")["results"][0]["snippet"]
    finally:
        conn.close()

    assert "<mark>don't</mark> <mark>worry</mark>".replace("'", "&#x27;") in snippet
