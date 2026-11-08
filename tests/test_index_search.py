"""End to end: index a small tree in-process, then search it."""

import os
import time
from pathlib import Path

import pytest

from sindex import control, db
from sindex.indexer import Indexer
from sindex.search import build_match, search

from .conftest import make_docx, make_pdf


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "docs"
    (root / "ru").mkdir(parents=True)
    (root / "ru" / "letter.txt").write_bytes(
        ("Дорогой друг, приезжай в Москву летом. Мы пойдём в Третьяковскую галерею и в театр. " * 3)
        .encode("cp1251"))
    (root / "greek.md").write_text("Ο καφές στην Αθήνα είναι πολύ καλός.", encoding="utf-8")
    (root / "fr.txt").write_text("Le café et la crème brûlée sont délicieux.", encoding="utf-8")
    (root / "ja.txt").write_text("東京都に住んでいます。京都にも行きました。", encoding="utf-8")
    (root / "he.txt").write_text("ש\u05b8\u05c1לו\u05b9ם עולם, זה מסמך בעברית.", encoding="utf-8")
    (root / "ar.txt").write_text("م\u064eر\u0652ح\u064eب\u064bا بالعالم، هذا مستند باللغة العربية.", encoding="utf-8")
    (root / "notes.txt").write_text("unrelated notes about gardening and tomatoes", encoding="utf-8")
    make_docx(root / "report.docx", ["Quarterly report", "Revenue grew in Москва"])
    make_pdf(root / "manual.pdf", ["Installation manual", "Turn the device on"])
    (root / "image.png").write_bytes(b"\x89PNG not indexed")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "dep.js").write_text("excluded tomatoes", encoding="utf-8")
    return root


def run_index(cfg, root: Path) -> None:
    code = Indexer(cfg, [str(root)], until_idle=True, limits=False).run()
    assert code == 0


def rescan(cfg, root: Path) -> None:
    conn = db.connect(cfg.db_path)
    db.enqueue(conn, str(root), db.STAGE_DIR, reason="test")
    conn.close()
    run_index(cfg, root)


def names(cfg, query, **kw):
    conn = db.connect(cfg.db_path, readonly=True)
    try:
        res = search(conn, query, **kw)
    finally:
        conn.close()
    assert res["error"] is None, res["error"]
    return {r["name"] for r in res["results"]}


@pytest.fixture
def indexed(cfg, tree):
    run_index(cfg, tree)
    return cfg, tree


@pytest.mark.parametrize("query,expected", [
    ("Москву", {"letter.txt"}),
    ("москва", {"report.docx"}),
    ("Третьяков*", {"letter.txt"}),
    ('"пойдём в Третьяковскую"', {"letter.txt"}),
    ("καφές", {"greek.md"}),
    ("καφες", {"greek.md"}),          # without tonos
    ("ΑΘΗΝΑ", {"greek.md"}),          # case-insensitive Greek
    ("cafe creme", {"fr.txt"}),       # without accents
    ("crème brûlée", {"fr.txt"}),
    ("東京", {"ja.txt"}),
    ("京都", {"ja.txt"}),
    ("東京都", {"ja.txt"}),
    ("大阪", set()),
    ("שלום", {"he.txt"}),             # niqqud in the document
    ("مرحبا", {"ar.txt"}),            # harakat in the document
    ("tomatoes", {"notes.txt"}),      # node_modules excluded
    ("revenue", {"report.docx"}),
    ("installation manual", {"manual.pdf"}),
    ("tomatoes OR revenue", {"notes.txt", "report.docx"}),
    ("notes -gardening", set()),
])
def test_queries(indexed, query, expected):
    cfg, _ = indexed
    assert names(cfg, query) == expected


def test_filters(indexed):
    cfg, tree = indexed
    assert names(cfg, "Москву", path=str(tree / "ru")) == {"letter.txt"}
    assert names(cfg, "Москву", path=str(tree / "nothing")) == set()
    assert names(cfg, "revenue OR tomatoes", exts=["docx"]) == {"report.docx"}
    assert names(cfg, "tomatoes", after="2999-01-01") == set()
    assert names(cfg, "tomatoes", after="1900-01-01", before="9999-12-30") == {"notes.txt"}


def test_statuses_and_encoding(indexed):
    cfg, tree = indexed
    conn = db.connect(cfg.db_path, readonly=True)
    rows = {name: (status, enc) for name, status, enc in conn.execute("SELECT name, status, encoding FROM files")}
    assert rows["letter.txt"][0] == "ok" and rows["letter.txt"][1].replace("_", "").lower() in ("cp1251", "windows1251")
    assert "image.png" not in rows and "dep.js" not in rows
    assert conn.execute("SELECT count(*) FROM queue").fetchone()[0] == 0


def test_snippet_is_readable(indexed):
    cfg, _ = indexed
    conn = db.connect(cfg.db_path, readonly=True)
    res = search(conn, "京都")
    snippet = res["results"][0]["snippet"]
    plain = snippet.replace("<mark>", "").replace("</mark>", "")
    assert "<mark>京都</mark>" in snippet
    assert "\ue000" not in snippet, "internal bigram separators must not leak into snippets"
    assert "東京都" in plain


def test_modify_delete_and_add(indexed):
    cfg, tree = indexed
    time.sleep(0.05)
    (tree / "notes.txt").write_text("now about cucumbers", encoding="utf-8")
    os.remove(tree / "fr.txt")
    (tree / "new.txt").write_text("brand new zucchini", encoding="utf-8")
    rescan(cfg, tree)
    assert names(cfg, "tomatoes") == set()
    assert names(cfg, "cucumbers") == {"notes.txt"}
    assert names(cfg, "cafe") == set()
    assert names(cfg, "zucchini") == {"new.txt"}


def test_deleted_directory(indexed):
    cfg, tree = indexed
    for f in (tree / "ru").iterdir():
        f.unlink()
    (tree / "ru").rmdir()
    rescan(cfg, tree)
    assert names(cfg, "Москву") == set()


def test_reindex_request_and_too_big(indexed):
    cfg, tree = indexed
    cfg.max_text_mb = 0  # everything text is now "too big"
    control.request_reindex(cfg, str(tree))
    run_index(cfg, tree)
    conn = db.connect(cfg.db_path, readonly=True)
    status = dict(conn.execute("SELECT name, status FROM files"))
    assert status["notes.txt"] == "too_big"
    assert names(cfg, "tomatoes") == set()


def test_truncation(cfg, tmp_path):
    root = tmp_path / "t"
    root.mkdir()
    (root / "long.txt").write_text("alpha " * 5000 + "omega", encoding="utf-8")
    cfg.max_chars_per_file = 1000
    cfg.chunk_chars = 300
    run_index(cfg, root)
    conn = db.connect(cfg.db_path, readonly=True)
    assert conn.execute("SELECT status FROM files").fetchone()[0] == "truncated"
    assert names(cfg, "alpha") == {"long.txt"} and names(cfg, "omega") == set()


def test_build_match_escapes_fts_syntax():
    assert build_match('a"b') is not None
    assert build_match("NEAR( x") is not None
    assert build_match("***") is None
    assert build_match("-only") is None
    assert build_match("OR OR") is None


def test_build_match_syntax():
    # a bare word is a prefix, a quoted phrase only itself
    assert build_match("a b") == '"a"* "b"*'
    assert build_match("a b OR c* -d") == '("a"* "b"* OR "c"*) NOT ("d"*)'
    assert build_match('"x y" OR') == '"x y"'
    assert build_match('"x y"* -"z w"') == '("x y"*) NOT ("z w")'
    assert build_match("OR a OR OR b") == '"a"* OR "b"*'
    assert build_match("*a OR b") == '"b"*'  # a group with a wild term is left to the scan
    assert build_match("*a b") is None


@pytest.fixture
def named(cfg, tmp_path):
    root = tmp_path / "docs"
    (root / "Invoices2024").mkdir(parents=True)
    (root / "Invoices2024" / "scan.txt").write_bytes(b"\x00\x01binary")  # no text at all
    (root / "Invoices2024" / "notes.txt").write_text("the word zebra", encoding="utf-8")
    (root / "café menu.txt").write_text("nothing special", encoding="utf-8")
    (root / "Мой отчёт.txt").write_text("nothing special", encoding="utf-8")
    (root / "zebra-plan.txt").write_text("plan", encoding="utf-8")
    run_index(cfg, root)
    return cfg


def test_names_are_not_searched_by_default(named):
    assert names(named, "invoices2024") == set()
    assert names(named, "menu") == set()


def test_names_finds_folder_and_file_names(named):
    assert names(named, "invoices2024", names=True) == {"scan.txt", "notes.txt"}
    assert names(named, "menu", names=True) == {"café menu.txt"}


def test_names_ignore_case_and_accents(named):
    assert names(named, "CAFE", names=True) == {"café menu.txt"}
    assert names(named, "отчёт", names=True) == {"Мой отчёт.txt"}
    assert names(named, "ОТЧЕТ", names=True) == {"Мой отчёт.txt"}


def test_names_use_the_query_syntax(named):
    # all words must be in the path (a word may sit in the folder and another in the file)
    assert names(named, "invoices2024 scan", names=True) == {"scan.txt"}
    assert names(named, '"café menu"', names=True) == {"café menu.txt"}
    assert names(named, "menu OR scan", names=True) == {"café menu.txt", "scan.txt"}
    assert names(named, "invoices2024 -scan", names=True) == {"notes.txt"}
    assert names(named, "menu*", names=True) == {"café menu.txt"}


def test_names_come_after_content_matches_and_respect_filters(named):
    conn = db.connect(named.db_path, readonly=True)
    res = search(conn, "zebra", names=True)
    assert [(r["name"], r["hits"]) for r in res["results"]] == [("notes.txt", 1), ("zebra-plan.txt", 0)]
    assert res["results"][0]["snippet"] and not res["results"][1]["snippet"]
    assert res["total"] == 2
    root = min(r[0] for r in conn.execute("SELECT dir FROM files"))  # the parent of the other folders
    only_txt = search(conn, "zebra", exts=["txt"], path=root, names=True)
    assert {r["name"] for r in only_txt["results"]} == {"notes.txt", "zebra-plan.txt"}
    assert {r["name"] for r in search(conn, "zebra", exts=["pdf"], names=True)["results"]} == set()

