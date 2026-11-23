"""index_all_names: files whose content cannot be read are listed by folder and name in `names`, without text,
size, date or a queue row."""

import os

import pytest

from sindex import db
from sindex.indexer import Indexer
from sindex.search import search
from sindex.usn import REASON_CREATE, REASON_DELETE, Change

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
    """file name -> status, with the files of `names` as name_only."""
    conn = db.connect(cfg.db_path, readonly=True)
    try:
        files = dict(conn.execute("SELECT name, status FROM files").fetchall())
        listed = dict.fromkeys((name for (name,) in conn.execute("SELECT name FROM names")), db.NAME_ONLY)
    finally:
        conn.close()
    assert not files.keys() & listed.keys(), "a file is in one table or the other"
    return files | listed


def folders(cfg):
    conn = db.connect(cfg.db_path, readonly=True)
    try:
        return [path for (path,) in conn.execute("SELECT path FROM dirs ORDER BY id")]
    finally:
        conn.close()


def found(cfg, query, **kw):
    conn = db.connect(cfg.db_path, readonly=True)
    try:
        res = search(conn, query, **kw)
    finally:
        conn.close()
    assert res["error"] is None, res["error"]
    return res["results"]


def test_unreadable_files_are_listed_by_name_only(mixed):
    cfg, root = mixed

    run_index(cfg, root)

    assert rows(cfg) == {"letter.doc": "name_only", "old.xls": "name_only", "notes.txt": "ok"}


def test_a_listed_file_has_no_text_no_files_row_and_no_queue_row(mixed):
    cfg, root = mixed
    run_index(cfg, root)

    conn = db.connect(cfg.db_path, readonly=True)
    assert conn.execute("SELECT count(*) FROM queue").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM files WHERE name IN ('letter.doc', 'old.xls')").fetchone()[0] == 0
    assert [c[1] for c in conn.execute("PRAGMA table_info(names)")] == ["dir_id", "name"]  # no size, no date
    conn.close()


def test_the_status_counts_the_listed_files(mixed):
    from sindex import control

    cfg, root = mixed
    run_index(cfg, root)
    conn = db.connect(cfg.db_path, readonly=True)
    stats = control.index_stats(conn, cfg)
    conn.close()

    assert stats["files"] == {"ok": 1, "name_only": 2}
    assert stats["total_files"] == 3


def test_the_names_search_finds_them_and_the_content_search_does_not(mixed):
    cfg, root = mixed
    run_index(cfg, root)

    assert names(cfg, ".doc", names_only=True) == {"letter.doc"}
    assert names(cfg, "xls", names=True) == {"old.xls"}
    assert names(cfg, "legacy") == set()  # no text was read from them
    assert names(cfg, "note") == {"notes.txt"}
    assert names(cfg, "docs", names_only=True) == {"letter.doc", "old.xls", "notes.txt"}  # the folder matches


def test_a_listed_file_is_found_with_regex_case_and_wildcards(mixed):
    cfg, root = mixed
    run_index(cfg, root)

    assert names(cfg, r"sub\\old\.xls$", names_only=True, regex=True) == {"old.xls"}
    assert names(cfg, "LETTER", names_only=True, case_sensitive=True) == set()
    assert names(cfg, "letter", names_only=True, case_sensitive=True) == {"letter.doc"}
    assert names(cfg, "*etter", names_only=True) == {"letter.doc"}


def test_folder_and_extension_filters_apply_to_listed_files(mixed):
    cfg, root = mixed
    run_index(cfg, root)

    assert names(cfg, "docs", names_only=True, path=str(root / "sub")) == {"old.xls"}
    assert names(cfg, "docs", names_only=True, exts=["doc"]) == {"letter.doc"}


def test_a_date_filter_leaves_listed_files_out(mixed):
    # the index keeps no date for them, and reading one from the disk for every name would be a scan of the disk
    cfg, root = mixed
    run_index(cfg, root)

    assert names(cfg, "docs", names_only=True, after="2000-01-01") == {"notes.txt"}


def test_an_exclusion_alone_in_names_lists_the_listed_files_too(mixed):
    cfg, root = mixed
    run_index(cfg, root)

    assert names(cfg, "-letter", names_only=True) == {"notes.txt", "old.xls"}
    assert names(cfg, "-letter", names=True) == {"notes.txt"}  # by content: a listed file's text is not known


def test_a_listed_file_shows_its_size_and_date_from_the_disk(mixed):
    cfg, root = mixed
    run_index(cfg, root)
    (root / "letter.doc").write_text("a longer legacy word file", encoding="utf-8")

    (hit,) = found(cfg, "letter", names_only=True)

    st = os.stat(root / "letter.doc")
    assert (hit["status"], hit["size"], hit["mtime"], hit["snippet"]) == ("name_only", st.st_size,
                                                                          st.st_mtime_ns / 1e9, "")


def test_a_listed_file_gone_from_the_disk_shows_no_size_or_date(mixed):
    cfg, root = mixed
    run_index(cfg, root)
    (root / "letter.doc").unlink()

    (hit,) = found(cfg, "letter", names_only=True)

    assert (hit["name"], hit["size"], hit["mtime"]) == ("letter.doc", None, None)


def test_sorting_by_size_reads_the_sizes_of_listed_files(mixed):
    cfg, root = mixed
    run_index(cfg, root)
    (root / "letter.doc").write_bytes(b"x" * 10_000)

    by_size = [r["name"] for r in found(cfg, "docs", names_only=True, sort="size_desc")]

    assert by_size == ["letter.doc", "old.xls", "notes.txt"]


def test_a_changed_listed_file_needs_no_write_and_a_deleted_one_is_dropped(mixed):
    cfg, root = mixed
    run_index(cfg, root)
    (root / "letter.doc").write_text("a longer legacy word file", encoding="utf-8")
    (root / "sub" / "old.xls").unlink()

    rescan(cfg, root)

    assert rows(cfg) == {"letter.doc": "name_only", "notes.txt": "ok"}


def test_nothing_is_listed_unless_the_switch_is_on(mixed):
    cfg, root = mixed
    cfg.index_all_names = False

    run_index(cfg, root)

    assert rows(cfg) == {"notes.txt": "ok"}


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
    assert folders(cfg) == [str(root)]  # sub held only a listed file


def test_a_file_that_becomes_readable_gets_its_text(mixed):
    cfg, root = mixed
    run_index(cfg, root)
    cfg.text_extensions = [*cfg.text_extensions, "doc"]

    run_index(cfg, root)

    assert rows(cfg)["letter.doc"] == "ok"
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


def test_a_folder_that_vanishes_takes_its_listed_names_and_its_number_with_it(mixed):
    cfg, root = mixed
    run_index(cfg, root)
    assert folders(cfg) == [str(root), str(root / "sub")]
    (root / "sub" / "old.xls").unlink()
    (root / "sub").rmdir()

    rescan(cfg, root)

    assert "old.xls" not in rows(cfg)
    assert folders(cfg) == [str(root)]


def test_a_folder_is_stored_once_for_all_its_files(cfg, tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    for n in range(5):
        (root / f"n{n}.txt").write_text(f"note {n}", encoding="utf-8")

    run_index(cfg, root)

    conn = db.connect(cfg.db_path, readonly=True)
    assert conn.execute("SELECT path FROM dirs").fetchall() == [(str(root),)]
    assert conn.execute("SELECT DISTINCT dir_id FROM files").fetchall() == [(1,)]
    conn.close()


def test_new_folders_are_numbered_in_path_order(cfg, tmp_path):
    # rows then go to the ends of the tables and their indexes, which keeps their pages full
    cfg.index_all_names = True
    root = tmp_path / "docs"
    for sub in ("c", "a", "b\\y", "b\\x"):
        (root / sub).mkdir(parents=True)
        (root / sub / "file.doc").write_bytes(b"\xd0\xcf")

    run_index(cfg, root)

    paths = folders(cfg)
    assert paths == sorted(paths, key=str.lower)
    assert len(paths) == 4  # a, b\x, b\y, c: b and the root hold no file of their own, so they get no number


def _indexer(cfg, root):
    idx = Indexer(cfg, [str(root)], until_idle=True, limits=False)
    idx.conn = db.connect(cfg.db_path)
    return idx


def test_a_journal_record_lists_or_drops_a_name_without_queueing_it(mixed):
    cfg, root = mixed
    run_index(cfg, root)
    (root / "sub" / "new.zip").write_bytes(b"PK")
    idx = _indexer(cfg, root)
    try:
        idx.conn.execute("BEGIN")
        idx._on_change(Change(str(root / "sub"), "new.zip", REASON_CREATE, is_dir=False))
        idx.conn.execute("COMMIT")
        assert rows(cfg)["new.zip"] == "name_only"

        (root / "sub" / "new.zip").unlink()
        (root / "sub" / "old.xls").unlink()
        idx.conn.execute("BEGIN")
        idx._on_change(Change(str(root / "sub"), "new.zip", REASON_DELETE, is_dir=False))
        idx._on_change(Change(str(root / "sub"), "old.xls", REASON_DELETE, is_dir=False))
        idx.conn.execute("COMMIT")

        assert idx.conn.execute("SELECT count(*) FROM queue").fetchone()[0] == 0
    finally:
        idx.conn.close()
        idx.runner.close()
    assert rows(cfg) == {"letter.doc": "name_only", "notes.txt": "ok"}
    assert folders(cfg) == [str(root)]  # its last listed file went, so did the folder
