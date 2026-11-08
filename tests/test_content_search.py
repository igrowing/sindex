"""Searching the stored text: words as prefixes, wildcards, case sensitivity and regular expressions."""

import pytest

from sindex import db
from sindex.search import find, has_wildcards, is_slow, search

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


STARTS_KYO = {"a1.txt", "a2.txt", "a3.txt", "a5.txt"}  # a word that starts with kyo: kyo, KYO, Kyo, kyoto
ALL_KYO = STARTS_KYO | {"a4.txt", "a6.txt"}  # and kyo anywhere in a word: tokyo, Tokyo


def test_a_word_finds_the_words_that_start_with_it(kyo):
    assert names(kyo, "kyo") == STARTS_KYO  # not tokyo: the word index answers it alone


def test_a_trailing_star_changes_nothing(kyo):
    assert names(kyo, "kyo*") == STARTS_KYO


def test_a_leading_star_finds_the_word_anywhere_in_a_word(kyo):
    assert names(kyo, "*kyo") == ALL_KYO


def test_whole_words_come_before_words_found_by_the_scan(kyo):
    found = [r["name"] for r in results(kyo, "kyo OR *kyo")["results"]]

    assert set(found[:4]) == STARTS_KYO
    assert set(found[4:]) == {"a4.txt", "a6.txt"}


def test_a_star_or_question_mark_inside_a_term_is_a_wildcard(kyo):
    assert names(kyo, "t?kyo") == {"a4.txt", "a6.txt"}  # exactly one letter between t and kyo
    assert names(kyo, "tt?kyo") == set()
    assert names(kyo, "to?yo") == {"a4.txt", "a6.txt"}
    assert names(kyo, "ky*to") == {"a5.txt"}
    assert names(kyo, "k?o") == STARTS_KYO  # at the start of a word, like any word
    assert names(kyo, "??kyo") == {"a4.txt", "a6.txt"}  # two letters before kyo
    assert names(kyo, "?kyo") == set()


def test_a_question_mark_at_the_end_is_punctuation(kyo):
    assert names(kyo, "kyo?") == STARTS_KYO


def test_case_sensitive_means_the_exact_letters_in_the_contents(kyo):
    assert names(kyo, "kyo", case_sensitive=True) == {"a1.txt", "a5.txt"}
    assert names(kyo, "Kyo", case_sensitive=True) == {"a3.txt"}
    assert names(kyo, "KYO", case_sensitive=True) == {"a2.txt"}
    assert names(kyo, "*kyo", case_sensitive=True) == {"a1.txt", "a4.txt", "a5.txt", "a6.txt"}  # Tokyo too


def test_case_sensitive_keeps_the_boolean_query_syntax(kyo):
    assert names(kyo, "Kyo OR KYO", case_sensitive=True) == {"a2.txt", "a3.txt"}
    assert names(kyo, "visit today", case_sensitive=True) == {"a1.txt", "a2.txt", "a3.txt", "a4.txt", "a5.txt"}


def test_a_case_sensitive_exclusion_only_drops_the_exact_letters(kyo):
    # the word index cannot see case: "-KYO" must not drop "kyo" before the letters are compared
    assert names(kyo, "visit -KYO", case_sensitive=True) == {"a1.txt", "a3.txt", "a4.txt", "a5.txt"}
    assert names(kyo, "visit -KYO") == {"a4.txt"}  # without case sensitivity kyo, KYO, Kyo and kyoto are one


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
    assert names(kyo, "kyo", names=True) == STARTS_KYO | {"kyo_name.txt"}


def test_a_hit_found_by_the_scan_gets_a_highlighted_snippet(kyo):
    by_name = {r["name"]: r for r in results(kyo, "*kyo")["results"]}

    assert "<mark>tokyo</mark>" in by_name["a4.txt"]["snippet"]
    assert "<mark>kyo</mark>to" in by_name["a5.txt"]["snippet"]


def test_the_scan_can_be_left_out(kyo):
    # the Explorer search connector asks on every keystroke and wants the instant index-only answer
    assert names(kyo, "kyo", scan=False) == STARTS_KYO
    assert names(kyo, "*kyo", scan=False) == set()  # a wild term finds nothing without the scan
    assert names(kyo, "*kyo OR cafe", scan=False) == {"a7.txt"}


def test_total_and_capping_count_files(kyo, monkeypatch):
    monkeypatch.setattr("sindex.search.HIT_LIMIT", 4)

    res = results(kyo, "*kyo")

    assert res["capped"] and res["total"] == 4


NO_KYO = {"a4.txt", "a6.txt", "a7.txt", "a8.txt", "kyo_name.txt"}  # the files without a word that starts with kyo


def test_an_exclusion_alone_lists_the_files_without_it(kyo):
    assert names(kyo, "-kyo") == NO_KYO  # not "Type some words": kyoto, KYO and Kyo are excluded too
    assert results(kyo, "-kyo")["error"] is None


def test_an_exclusion_alone_can_have_several_terms(kyo):
    assert names(kyo, "-kyo -cafe") == NO_KYO - {"a7.txt"}


def test_an_exclusion_alone_with_a_leading_star_excludes_the_word_anywhere(kyo):
    assert names(kyo, "-*kyo") == {"a7.txt", "a8.txt", "kyo_name.txt"}


def test_an_exclusion_alone_with_case_sensitive_excludes_only_the_exact_letters(kyo):
    assert names(kyo, "-kyo", case_sensitive=True) == NO_KYO | {"a2.txt", "a3.txt"}  # KYO and Kyo stay


def test_an_exclusion_alone_also_excludes_names_when_asked(kyo):
    assert names(kyo, "-kyo", names=True) == NO_KYO - {"kyo_name.txt"}  # it has kyo in its name


def test_an_exclusion_alone_marks_its_results_as_having_no_hit(kyo):
    res = results(kyo, "-kyo")

    assert res["negative_only"] is True
    assert all(r["hits"] == 0 and r["snippet"] == "" for r in res["results"])
    assert results(kyo, "kyo")["negative_only"] is False


def test_an_exclusion_alone_respects_the_other_filters(kyo, tmp_path):
    assert names(kyo, "-kyo", exts=["txt"], path=str(tmp_path / "docs")) == NO_KYO
    assert names(kyo, "-kyo", path=str(tmp_path / "elsewhere")) == set()


def test_an_exclusion_next_to_a_word(kyo):
    assert names(kyo, "-kyo visit") == {"a4.txt"}  # tokyo has no word that starts with kyo
    assert names(kyo, "-*kyo visit") == set()  # every file that has "visit" has kyo somewhere in a word
    assert names(kyo, "visit -tokyo") == STARTS_KYO
    assert names(kyo, "-kyo visit", case_sensitive=True) == {"a2.txt", "a3.txt", "a4.txt"}


def test_an_exclusion_alone_needs_no_scan(kyo):
    # the word index answers "everything but", so the Explorer connector can ask for it too
    assert names(kyo, "-kyo", scan=False) == NO_KYO
    assert "needs the scan" in results(kyo, "-*kyo", scan=False)["error"]


def test_wildcards_and_slow_searches_are_recognised():
    assert not has_wildcards("kyo kyo* kyo? -kyo \"a b\"")  # trailing ones change nothing
    assert has_wildcards("*kyo") and has_wildcards("t?kyo") and has_wildcards("ky*to") and has_wildcards("-*kyo")
    assert not is_slow("kyo", regex=False, case_sensitive=False)
    assert is_slow("*kyo", regex=False, case_sensitive=False)
    assert is_slow("kyo", regex=True, case_sensitive=False)
    assert is_slow("kyo", regex=False, case_sensitive=True)


def test_progress_hands_out_what_the_scan_found_so_far(kyo, monkeypatch):
    monkeypatch.setattr("sindex.search.PROGRESS_EVERY_S", 0)  # report after every hit
    snapshots = []
    conn = db.connect(kyo.db_path, readonly=True)
    try:
        found = find(conn, "*kyo", progress=snapshots.append)
    finally:
        conn.close()

    assert [len(s.best) for s in snapshots] == list(range(1, len(found.best) + 1))
    assert all(s is not found for s in snapshots), "a snapshot is a copy the search does not change"
    assert not any(s.by_proximity for s in snapshots)


def test_progress_while_checking_case_keeps_the_ranking_of_a_plain_search(kyo, monkeypatch):
    monkeypatch.setattr("sindex.search.PROGRESS_EVERY_S", 0)
    snapshots = []
    conn = db.connect(kyo.db_path, readonly=True)
    try:
        streamed = find(conn, "visit", case_sensitive=True, progress=snapshots.append)
        ranked = find(conn, "visit", case_sensitive=True)
    finally:
        conn.close()

    assert snapshots, "the case check handed out its hits while it ran"
    assert list(streamed.best) == list(ranked.best)
    assert streamed.indexed == ranked.indexed and streamed.hits == ranked.hits


@pytest.fixture
def punct(cfg, tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    for name, text in {
        "url.txt": "see http://www.nasdaq.com/symbol/kyo for the quote",
        "paren.txt": "the code beegee-tokyo) was listed",
        "word.txt": "plain kyo) at the end",
        "cpp.txt": "I write c++ every day",
        "c.txt": "the c language is old",
        "cafe.txt": "un café (noir) s'il vous plaît",
        "cafe2.txt": "un café au lait",
    }.items():
        (root / name).write_text(text, encoding="utf-8")
    run_index(cfg, root)
    return cfg


def test_punctuation_in_the_query_must_be_in_the_text(punct):
    # the word index drops the ")" and would find the URL that ends in the word kyo
    assert names(punct, "kyo)") == {"word.txt"}
    assert names(punct, "c++") == {"cpp.txt"}  # not every file that has the letter c


def test_punctuation_after_a_word_inside_another_word_needs_a_leading_star(punct):
    assert names(punct, "tokyo)") == {"paren.txt"}  # the hyphen starts the word
    assert names(punct, "okyo)") == set()
    assert names(punct, "*okyo)") == {"paren.txt"}


def test_a_word_without_punctuation_still_ignores_accents_next_to_a_punctuated_one(punct):
    assert names(punct, "cafe (noir") == {"cafe.txt"}
    assert names(punct, "cafe") == {"cafe.txt", "cafe2.txt"}


def test_punctuation_hits_get_a_snippet_around_the_text_that_matched(punct):
    by_name = {r["name"]: r for r in results(punct, "kyo)")["results"]}

    assert "<mark>kyo)</mark>" in by_name["word.txt"]["snippet"]


def test_cjk_text_is_found_in_the_middle_of_a_run(cfg, tmp_path):
    # CJK has no spaces between words, so the start of a "word" is not where a term must begin
    root = tmp_path / "docs"
    root.mkdir()
    (root / "tokyo.txt").write_text("東京都に住む", encoding="utf-8")  # 東京都に住む
    run_index(cfg, root)

    assert names(cfg, "京都") == {"tokyo.txt"}  # 京都, in the middle
    assert names(cfg, "京都", case_sensitive=True) == {"tokyo.txt"}
