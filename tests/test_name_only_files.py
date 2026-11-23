"""index_all_names: files whose content cannot be read are listed by their name, without text and without a queue."""

import os

import pytest

from sindex import db
from sindex.search import search

from .test_index_search import names, rescan, run_index


@pytest.fixture
def mixed(cfg, tmp_path):
    cfg.index_all_names = True
    root = tmp_path / "docs"
    (root / "sub").mkdir(parents=True)
    (root / "letter.doc").write_text("legacy word", encoding="utf-8")
    (root / "sub" / "old.xls").write_bytes(b"\xd0\xcf\x11\xe0 legacy excel")
    (root / "notes.txt").write_text("a readable note", encoding="utf-8")
    (root / "setup.exe").write_bytes(b"MZ")  # an excluded extension is never listed
    return cfg, root


def rows(cfg):
    conn = db.connect(cfg.db_path, readonly=True)
    try:
        return dict(conn.execute("SELECT name, status FROM files").fetchall())
    finally:
        conn.close()


def test_unreadable_files_are_listed_by_name_only(mixed):
    cfg, root = mixed

    run_index(cfg, root)

    assert rows(cfg) == {"letter.doc": "name_only", "old.xls": "name_only", "notes.txt": "ok"}


def test_a_name_only_file_has_no_text_and_no_queue_row(mixed):
    cfg, root = mixed
    run_index(cfg, root)

    conn = db.connect(cfg.db_path, readonly=True)
    assert conn.execute("SELECT count(*) FROM queue").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM chunks WHERE rowid >> ? IN "
                        "(SELECT id FROM files WHERE status = 'name_only')", (db.CHUNK_BITS,)).fetchone()[0] == 0
    conn.close()


def test_the_names_search_finds_them_and_the_content_search_does_not(mixed):
    cfg, root = mixed
    run_index(cfg, root)

    assert names(cfg, ".doc", names_only=True) == {"letter.doc"}
    assert names(cfg, "xls", names=True) == {"old.xls"}
    assert names(cfg, "legacy") == set()  # no text was read from them
    assert names(cfg, "note") == {"notes.txt"}


def test_nothing_is_listed_unless_the_switch_is_on(mixed):
    cfg, root = mixed
    cfg.index_all_names = False

    run_index(cfg, root)

    assert rows(cfg) == {"notes.txt": "ok"}


def test_a_changed_file_is_updated_and_a_deleted_one_dropped(mixed):
    cfg, root = mixed
    run_index(cfg, root)
    (root / "letter.doc").write_text("a longer legacy word file", encoding="utf-8")
    (root / "sub" / "old.xls").unlink()

    rescan(cfg, root)

    conn = db.connect(cfg.db_path, readonly=True)
    size = conn.execute("SELECT size FROM files WHERE name = 'letter.doc'").fetchone()[0]
    conn.close()
    assert size == os.path.getsize(root / "letter.doc")
    assert rows(cfg) == {"letter.doc": "name_only", "notes.txt": "ok"}


def test_turning_the_switch_on_or_off_lists_or_drops_the_names_at_the_next_start(mixed):
    cfg, root = mixed
    cfg.index_all_names = False
    run_index(cfg, root)

    cfg.index_all_names = True
    run_index(cfg, root)
    assert set(rows(cfg)) == {"letter.doc", "old.xls", "notes.txt"}

    cfg.index_all_names = False
    run_index(cfg, root)
    assert rows(cfg) == {"notes.txt": "ok"}


def test_a_file_that_becomes_readable_gets_its_text(mixed):
    cfg, root = mixed
    run_index(cfg, root)
    cfg.text_extensions = [*cfg.text_extensions, "doc"]

    run_index(cfg, root)

    assert rows(cfg)["letter.doc"] != "name_only"
    assert names(cfg, "legacy") == {"letter.doc"}


def test_a_file_that_stops_being_readable_loses_its_text(mixed):
    cfg, root = mixed
    cfg.text_extensions = [*cfg.text_extensions, "doc"]
    run_index(cfg, root)
    assert names(cfg, "legacy") == {"letter.doc"}
    cfg.text_extensions = [e for e in cfg.text_extensions if e != "doc"]

    run_index(cfg, root)

    assert rows(cfg)["letter.doc"] == "name_only"
    assert names(cfg, "legacy") == set()


def test_a_folder_that_vanishes_takes_its_listed_names_with_it(mixed):
    cfg, root = mixed
    run_index(cfg, root)
    (root / "sub" / "old.xls").unlink()
    (root / "sub").rmdir()

    rescan(cfg, root)

    assert "old.xls" not in rows(cfg)


def test_a_name_only_result_says_so_and_has_no_snippet(mixed):
    cfg, root = mixed
    run_index(cfg, root)
    conn = db.connect(cfg.db_path, readonly=True)
    res = search(conn, "letter", names_only=True)
    conn.close()

    assert [(r["name"], r["status"], r["snippet"]) for r in res["results"]] == [("letter.doc", "name_only", "")]
