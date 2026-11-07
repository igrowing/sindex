"""Searching the stored text: words inside words, wildcards, case sensitivity and regular expressions."""

import pytest

from sindex import db
from sindex.search import search

from .test_index_search import names, run_index


@pytest.fixture
def kyo(cfg, tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    for name, text in {
        "a1.txt": "visit kyo today", "a2.txt": "visit KYO today", "a3.txt": "visit Kyo today",
        "a4.txt": "visit tokyo today", "a5.txt": "visit kyoto today", "a6.txt": "Tokyo, Haneda airport",
        "a7.txt": "un café noir", "a8.txt": "nothing relevant", "kyo_name.txt": "also nothing",
    }.items():
        (root / name).write_text(text, encoding="utf-8")
    run_index(cfg, root)
    return cfg


def results(cfg, query, **kw):
    conn = db.connect(cfg.db_path, readonly=True)
    try:
        return search(conn, query, **kw)
    finally:
        conn.close()


ALL_KYO = {"a1.txt", "a2.txt", "a3.txt", "a4.txt", "a5.txt", "a6.txt"}


def test_a_word_is_also_found_inside_other_words(kyo):  # bug 2
    assert names(kyo, "kyo") == ALL_KYO


def test_a_trailing_star_finds_the_word_inside_other_words_too(kyo):  # bug 3
    assert names(kyo, "kyo*") == ALL_KYO


def test_a_leading_star_is_not_ignored(kyo):
    assert names(kyo, "*kyo") == ALL_KYO


def test_whole_words_come_before_words_found_inside_others(kyo):
    found = [r["name"] for r in results(kyo, "kyo")["results"]]

    assert set(found[:3]) == {"a1.txt", "a2.txt", "a3.txt"}
    assert set(found[3:]) == {"a4.txt", "a5.txt", "a6.txt"}


def test_a_star_or_question_mark_inside_a_term_is_a_wildcard(kyo):
    assert names(kyo, "t?kyo") == {"a4.txt", "a6.txt"}  # exactly one letter between t and kyo
    assert names(kyo, "tt?kyo") == set()
    assert names(kyo, "to?yo") == {"a4.txt", "a6.txt"}
    assert names(kyo, "ky*to") == {"a5.txt"}
    assert names(kyo, "k?o") == ALL_KYO


def test_a_question_mark_at_the_end_is_punctuation(kyo):
    assert names(kyo, "kyo?") == ALL_KYO


def test_case_sensitive_means_the_exact_letters_in_the_contents(kyo):  # bug 1
    assert names(kyo, "kyo", case_sensitive=True) == {"a1.txt", "a4.txt", "a5.txt", "a6.txt"}
    assert names(kyo, "Kyo", case_sensitive=True) == {"a3.txt"}
    assert names(kyo, "KYO", case_sensitive=True) == {"a2.txt"}


def test_case_sensitive_keeps_the_boolean_query_syntax(kyo):
    assert names(kyo, "Kyo OR KYO", case_sensitive=True) == {"a2.txt", "a3.txt"}
    assert names(kyo, "kyo -tokyo", case_sensitive=True) == {"a1.txt", "a5.txt", "a6.txt"}  # "Tokyo" is not "tokyo"
    assert names(kyo, "visit today", case_sensitive=True) == {"a1.txt", "a2.txt", "a3.txt", "a4.txt", "a5.txt"}


def test_words_still_match_without_accents(kyo):
    assert names(kyo, "cafe") == {"a7.txt"}  # the word index folds accents
    assert names(kyo, "café") == {"a7.txt"}


def test_regex_searches_the_contents(kyo):
    assert names(kyo, r"to(ky)o", regex=True) == {"a4.txt", "a6.txt"}
    assert names(kyo, r"to(ky)o", regex=True, case_sensitive=True) == {"a4.txt"}
    assert names(kyo, r"\bkyo\b", regex=True) == {"a1.txt", "a2.txt", "a3.txt"}
    assert names(kyo, r"\bkyo\b", regex=True, case_sensitive=True) == {"a1.txt"}


def test_regex_does_not_search_names_unless_asked(kyo):
    assert names(kyo, "kyo_name", regex=True) == set()
    assert names(kyo, "kyo_name", regex=True, names=True) == {"kyo_name.txt"}
    assert names(kyo, r"kyo_name\.txt$", regex=True, names=True, case_sensitive=True) == {"kyo_name.txt"}
    assert names(kyo, r"KYO_name\.txt$", regex=True, names=True, case_sensitive=True) == set()


def test_names_are_added_only_with_the_names_box(kyo):
    assert "kyo_name.txt" not in names(kyo, "kyo")
    assert names(kyo, "kyo", names=True) == ALL_KYO | {"kyo_name.txt"}


def test_a_hit_inside_a_word_gets_a_highlighted_snippet(kyo):
    by_name = {r["name"]: r for r in results(kyo, "kyo")["results"]}

    assert "to<mark>kyo</mark>" in by_name["a4.txt"]["snippet"]
    assert "<mark>kyo</mark>to" in by_name["a5.txt"]["snippet"]


def test_the_scan_can_be_left_out(kyo):
    # the Explorer search connector asks on every keystroke and wants the instant index-only answer
    assert names(kyo, "kyo", contains=False) == {"a1.txt", "a2.txt", "a3.txt"}


def test_total_and_capping_count_files(kyo, monkeypatch):
    monkeypatch.setattr("sindex.search.HIT_LIMIT", 4)

    res = results(kyo, "kyo")

    assert res["capped"] and res["total"] == 4


NO_KYO = {"a7.txt", "a8.txt", "kyo_name.txt"}  # the files whose text has no "kyo" in it, in any case or word


def test_an_exclusion_alone_lists_the_files_without_it(kyo):
    assert names(kyo, "-kyo") == NO_KYO  # not "Type some words": tokyo, kyoto and Kyo are excluded too
    assert results(kyo, "-kyo")["error"] is None


def test_an_exclusion_alone_can_have_several_terms(kyo):
    assert names(kyo, "-kyo -cafe") == {"a8.txt", "kyo_name.txt"}


def test_an_exclusion_alone_with_case_sensitive_excludes_only_the_exact_letters(kyo):
    # "kyo" is inside a1, a4, a5 and "Tokyo" (a6); KYO and Kyo are different letters
    assert names(kyo, "-kyo", case_sensitive=True) == NO_KYO | {"a2.txt", "a3.txt"}


def test_an_exclusion_alone_also_excludes_names_when_asked(kyo):
    assert names(kyo, "-kyo", names=True) == {"a7.txt", "a8.txt"}  # kyo_name.txt has it in its name


def test_an_exclusion_alone_marks_its_results_as_having_no_hit(kyo):
    res = results(kyo, "-kyo")

    assert res["negative_only"] is True
    assert all(r["hits"] == 0 and r["snippet"] == "" for r in res["results"])
    assert results(kyo, "kyo")["negative_only"] is False


def test_an_exclusion_alone_respects_the_other_filters(kyo, tmp_path):
    assert names(kyo, "-kyo", exts=["txt"], path=str(tmp_path / "docs")) == NO_KYO
    assert names(kyo, "-kyo", path=str(tmp_path / "elsewhere")) == set()


def test_an_exclusion_next_to_a_word_also_drops_the_words_found_inside_others(kyo):
    # every file that has "visit" also has kyo, tokyo or kyoto
    assert names(kyo, "-kyo visit") == set()
    assert names(kyo, "visit -tokyo") == {"a1.txt", "a2.txt", "a3.txt", "a5.txt"}  # a6 says "Tokyo" but no "visit"
    assert names(kyo, "-kyo visit", case_sensitive=True) == {"a2.txt", "a3.txt"}


def test_an_exclusion_alone_needs_the_scan(kyo):
    # the Explorer connector goes without the scan and has no way to answer "everything but"
    assert results(kyo, "-kyo", contains=False)["error"] == "Type some words to search for."
