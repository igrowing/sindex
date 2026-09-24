"""USN journal against the real volume. Needs an elevated prompt; skipped otherwise."""

import os
from pathlib import Path

import pytest

from sindex import db
from sindex.indexer import Indexer
from sindex.search import search
from sindex.usn import REASON_DELETE, REASON_RENAME_NEW, Journal, JournalError, fs_type


def _journal(path: Path) -> Journal:
    letter = str(path)[:2]
    if fs_type(letter) != "NTFS":
        pytest.skip("not an NTFS volume")
    try:
        j = Journal(letter)
        j.query()
    except JournalError as e:
        pytest.skip(f"USN journal not accessible (run elevated): {e}")
    return j


def test_journal_reports_changes(tmp_path):
    j = _journal(tmp_path)
    try:
        jid, _, start = j.query()
        f = tmp_path / "usn_probe.txt"
        f.write_text("hello")
        f.rename(tmp_path / "usn_renamed.txt")
        os.remove(tmp_path / "usn_renamed.txt")
        changes = [c for c in j.read(start, jid) if c.parent_path and Path(c.parent_path) == tmp_path]
        by_name = {}
        for c in changes:
            by_name[c.name] = by_name.get(c.name, 0) | c.reason
        assert "usn_probe.txt" in by_name
        assert by_name["usn_renamed.txt"] & REASON_RENAME_NEW
        assert by_name["usn_renamed.txt"] & REASON_DELETE
        assert j.next_usn > start
    finally:
        j.close()


def test_indexer_follows_journal(cfg, tmp_path):
    _journal(tmp_path).close()
    root = tmp_path / "watched"
    root.mkdir()
    (root / "a.txt").write_text("first apricot", encoding="utf-8")
    Indexer(cfg, [str(root)], until_idle=True, limits=False).run()  # full scan, journal position saved

    (root / "a.txt").write_text("now banana", encoding="utf-8")
    (root / "b.txt").write_text("cherry", encoding="utf-8")
    (root / "sub").mkdir()
    (root / "sub" / "c.txt").write_text("damson", encoding="utf-8")
    (root / "sub").rename(root / "moved")
    Indexer(cfg, [str(root)], until_idle=True, limits=False).run()  # only reads the journal

    conn = db.connect(cfg.db_path, readonly=True)

    def found(query):
        return {r["name"] for r in search(conn, query)["results"]}

    assert found("apricot") == set()
    assert found("banana") == {"a.txt"}
    assert found("cherry") == {"b.txt"}
    paths = {r["path"] for r in search(conn, "damson")["results"]}
    assert paths == {str(root / "moved" / "c.txt")}
