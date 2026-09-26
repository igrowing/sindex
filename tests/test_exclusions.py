"""Exclusion by extension and by pattern, and what happens to files indexed before a rule was added."""

import logging
import os
from pathlib import Path

import pytest

from sindex import db
from sindex.filters import PathFilter, compile_globs
from sindex.indexer import Indexer
from sindex.search import search


def test_a_pattern_without_backslash_is_about_names_and_one_with_is_about_paths():
    names, by_name, by_path = compile_globs(["*.ENV", "secret*", "*/.ssh/*", "D:\\Private\\*", "Node_Modules", ".*"])

    assert names == {"node_modules"}  # a plain name needs no regular expression
    assert len(by_name) == 3 and len(by_path) == 2
    assert by_name[2].match(".git") and by_name[2].match(".hidden.txt") and not by_name[2].match("a.txt")
    assert by_name[0].match("prod.env") and by_name[0].match("PROD.Env") and not by_name[0].match("env.txt")
    assert by_name[1].match("secret-notes.txt") and not by_name[1].match("my-secret.txt")
    assert by_path[0].match("C:\\Users\\me\\.ssh\\id_rsa") and not by_path[0].match("C:\\Users\\me\\ssh\\x")
    assert by_path[1].match("d:\\private\\a\\b.txt") and not by_path[1].match("D:\\Privates\\a.txt")


@pytest.fixture
def filt(cfg):
    cfg.exclude_globs = ["*.bak", "*\\.ssh\\*", "D:\\Docs\\Private"]
    cfg.exclude_extensions = ["LOG", ".pem"]
    return PathFilter(cfg, ["D:\\"])


def test_globs_skip_files_and_folders_while_walking(filt):
    assert filt.skip_file_entry("D:\\Docs\\a.bak", "a.bak", 0)
    assert not filt.skip_file_entry("D:\\Docs\\a.txt", "a.txt", 0)
    assert filt.skip_dir_entry("D:\\Docs\\.ssh", ".ssh", 0)  # "*\.ssh\*" also covers the folder itself
    assert filt.skip_dir_entry("D:\\Docs\\Private", "Private", 0)  # a pattern that names a folder exactly
    assert not filt.skip_dir_entry("D:\\Docs\\Public", "Public", 0)


def test_globs_apply_to_everything_below_an_excluded_folder(filt):
    assert filt.dir_excluded("D:\\Docs\\Private")
    assert not filt.dir_excluded("D:\\Docs\\Public")
    assert filt.dir_excluded("D:\\Users\\me\\.ssh\\keys")
    assert filt.file_kind("D:\\Users\\me\\.ssh\\notes.txt") is None
    assert filt.file_kind("D:\\Docs\\a.txt") == "text"
    assert filt.file_kind("D:\\Docs\\a.bak") is None and filt.file_kind("D:\\Docs\\a.TXT.bak") is None


def test_extensions_are_excluded_whatever_their_case_or_dot(filt):
    assert filt.file_kind("D:\\Docs\\server.log") is None
    assert filt.file_kind("D:\\Docs\\SERVER.LOG") is None
    assert filt.file_kind("D:\\Docs\\notes.txt") == "text"


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "docs"
    for name in ("Private", "Public", ".ssh"):
        (root / name).mkdir(parents=True)
    (root / "Public" / "plan.txt").write_text("banana plan", encoding="utf-8")
    (root / "Public" / "server.log").write_text("banana log", encoding="utf-8")
    (root / "Public" / "secret-note.txt").write_text("banana secret", encoding="utf-8")
    (root / "Private" / "diary.txt").write_text("banana diary", encoding="utf-8")
    (root / ".ssh" / "config.txt").write_text("banana ssh", encoding="utf-8")
    return root


def index(cfg, root: Path) -> set[str]:
    assert Indexer(cfg, [str(root)], until_idle=True, limits=False).run() == 0
    conn = db.connect(cfg.db_path, readonly=True)
    try:
        return {r["name"] for r in search(conn, "banana")["results"]}
    finally:
        conn.close()


def test_everything_is_indexed_without_rules(cfg, tree):
    cfg.index_hidden = True
    cfg.exclude_globs = []  # the default already skips dotfolders like .ssh; this tests having none at all
    assert index(cfg, tree) == {"plan.txt", "server.log", "secret-note.txt", "diary.txt", "config.txt"}


def test_excluded_files_are_not_indexed(cfg, tree):
    cfg.index_hidden = True
    cfg.exclude_extensions = ["log"]
    cfg.exclude_globs = ["secret*", os.path.join(str(tree), "Private", "*"), "*\\.ssh\\*"]

    assert index(cfg, tree) == {"plan.txt"}


def test_a_new_rule_removes_what_was_indexed_before_and_only_rescans_when_rules_change(cfg, tree, caplog):
    cfg.index_hidden = True
    cfg.exclude_globs = []  # start from no rules, so the change below is the only thing that removes files
    assert len(index(cfg, tree)) == 5

    cfg.exclude_globs = ["*\\Private\\*"]
    cfg.exclude_extensions = ["log"]
    with caplog.at_level(logging.INFO, logger="sindex.indexer"):
        assert index(cfg, tree) == {"plan.txt", "secret-note.txt", "config.txt"}
    assert "indexing rules changed" in caplog.text

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="sindex.indexer"):
        assert index(cfg, tree) == {"plan.txt", "secret-note.txt", "config.txt"}
    assert "indexing rules changed" not in caplog.text  # remembered: no rescan the second time


def test_removing_a_rule_brings_the_files_back(cfg, tree):
    cfg.index_hidden = True
    cfg.exclude_extensions = ["log"]
    assert "server.log" not in index(cfg, tree)

    cfg.exclude_extensions = []

    assert "server.log" in index(cfg, tree)


def test_an_interrupted_rescan_is_repeated(cfg, tree, monkeypatch):
    # the rules are remembered only after the scans have run, so a restart in between does not lose the change
    cfg.index_hidden = True
    index(cfg, tree)
    cfg.exclude_extensions = ["log"]
    indexer = Indexer(cfg, [str(tree)], until_idle=True, limits=False)
    monkeypatch.setattr(indexer, "_full_scans", lambda: False)  # "crashes" before scanning
    monkeypatch.setattr(indexer, "_loop", lambda: 0)
    indexer.run()

    conn = db.connect(cfg.db_path, readonly=True)
    assert db.get_meta(conn, "rules") != indexer._rules()
    conn.close()
    assert "server.log" not in index(cfg, tree)


def test_a_wildcard_in_a_name_skips_files_and_folders_of_that_kind(cfg):
    cfg.exclude_globs = [".*", "tmp*"]
    cfg.index_hidden = True
    filt = PathFilter(cfg, ["D:\\"])

    assert filt.skip_dir_entry("D:\\Docs\\.git", ".git", 0) and filt.skip_dir_entry("D:\\Docs\\tmp1", "tmp1", 0)
    assert filt.skip_file_entry("D:\\Docs\\.env", ".env", 0)
    assert filt.dir_excluded("D:\\Docs\\tmp1\\deeper") and not filt.dir_excluded("D:\\Docs\\Temp")
    assert filt.file_kind("D:\\Docs\\.notes.txt") is None and filt.file_kind("D:\\Docs\\notes.txt") == "text"


def test_wildcard_extensions_are_not_indexed(cfg, tree):
    cfg.index_hidden = True
    cfg.exclude_extensions = ["lo*"]  # log

    assert "server.log" not in index(cfg, tree) and "plan.txt" in index(cfg, tree)
