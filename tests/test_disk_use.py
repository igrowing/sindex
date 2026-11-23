"""What takes the space in the index, by folder (control.disk_use, /api/usage, `sindex du`), and files.text_bytes."""

import json
import os
from urllib.parse import quote

import pytest

from sindex import cli, control, db

from .conftest import call
from .test_index_search import run_index


@pytest.fixture
def tree(cfg, tmp_path):
    r"""docs\top.txt, docs\a\{1,2}.txt, docs\b\c\deep.txt, and docs\x-y\other.txt next to docs\x\inner.txt."""
    root = tmp_path / "docs"
    files = {
        "top.txt": "t" * 100,
        "a/1.txt": "alpha " * 200,
        "a/2.txt": "beta " * 300,
        "b/c/deep.txt": "gamma " * 50,
        "x/inner.txt": "inner " * 10,
        "x-y/other.txt": "other " * 1000,  # sorts between x and x\inner unless the walk takes care
    }
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    run_index(cfg, root)
    return cfg, root


def stored(cfg) -> dict[str, int]:
    """file name -> bytes of its stored text, measured from the chunks."""
    conn = db.connect(cfg.db_path, readonly=True)
    try:
        return {name: db.stored_text_bytes(conn, fid) for fid, name in conn.execute("SELECT id, name FROM files")}
    finally:
        conn.close()


def use(cfg, path="", **kw):
    conn = db.connect(cfg.db_path, readonly=True)
    try:
        return control.disk_use(conn, cfg, path, **kw)
    finally:
        conn.close()


def test_indexing_records_the_size_of_the_stored_text(tree):
    cfg, _ = tree
    conn = db.connect(cfg.db_path, readonly=True)
    recorded = dict(conn.execute("SELECT name, text_bytes FROM files").fetchall())
    conn.close()

    assert recorded == stored(cfg)
    assert all(n > 0 for n in recorded.values())


def test_the_folders_below_a_folder_count_everything_below_them(tree):
    cfg, root = tree
    size = stored(cfg)

    res = use(cfg, str(root))

    rows = {r["name"]: r for r in res["rows"]}
    assert set(rows) == {"a", "b", "x", "x-y"}
    assert rows["a"]["text_bytes"] == size["1.txt"] + size["2.txt"] and rows["a"]["files"] == 2
    assert rows["b"]["text_bytes"] == size["deep.txt"] and rows["b"]["own_text_bytes"] == 0  # all of it in b\c
    assert rows["x"]["text_bytes"] == size["inner.txt"]  # not x-y's
    assert rows["x-y"]["text_bytes"] == size["other.txt"]
    assert res["here"]["text_bytes"] == size["top.txt"] and res["here"]["files"] == 1
    assert [r["name"] for r in res["rows"]] == sorted(rows, key=lambda n: rows[n]["text_bytes"], reverse=True)
    assert res["chain"][-1] == str(root) and res["chain"][0].endswith(":\\")


def test_the_shares_add_up_and_the_index_estimate_follows_the_text(tree):
    cfg, root = tree
    res = use(cfg, str(root))

    total = sum(r["text_bytes"] for r in res["rows"]) + res["here"]["text_bytes"]
    assert total == res["text_bytes"]
    assert sum(r["share"] for r in res["rows"]) + res["here"]["share"] == pytest.approx(1.0)
    for r in res["rows"]:
        assert r["index_bytes"] == round(r["text_bytes"] * res["db_bytes"] / res["text_bytes"])


def test_the_top_level_lists_the_drives(tree):
    cfg, root = tree
    res = use(cfg)

    assert [r["path"] for r in res["rows"]] == [os.path.splitdrive(str(root))[0] + "\\"]
    assert res["rows"][0]["text_bytes"] == res["text_bytes"]
    assert res["chain"] == [] and res["here"] is None


def test_the_biggest_folders_anywhere_include_the_folders_above(tree):
    cfg, root = tree
    size = stored(cfg)

    res = use(cfg, anywhere=True, limit=50)

    by_path = {r["path"]: r for r in res["rows"]}
    assert by_path[str(root)]["text_bytes"] == sum(size.values())
    assert by_path[str(root / "b" / "c")]["text_bytes"] == size["deep.txt"]
    texts = [r["text_bytes"] for r in res["rows"]]
    assert texts == sorted(texts, reverse=True)


def test_the_biggest_folders_anywhere_leave_out_folders_that_only_lead_to_one_subfolder(tree):
    cfg, root = tree

    paths = {r["path"] for r in use(cfg, anywhere=True, limit=50)["rows"]}

    assert str(root / "b") not in paths  # nothing of its own, only b\c: the same numbers twice
    assert str(root.parent) not in paths  # the folders above docs, likewise
    assert str(root) in paths and str(root / "b" / "c") in paths


def test_the_biggest_folders_anywhere_keep_only_the_limit(tree):
    cfg, root = tree

    res = use(cfg, anywhere=True, limit=2)

    assert len(res["rows"]) == 2
    assert res["rows"][1]["text_bytes"] >= use(cfg, str(root))["rows"][0]["text_bytes"]


def test_listed_names_are_counted(cfg, tmp_path):
    cfg.index_all_names = True
    root = tmp_path / "docs"
    (root / "old").mkdir(parents=True)
    (root / "old" / "letter.doc").write_bytes(b"\xd0\xcf")
    (root / "old" / "sheet.xls").write_bytes(b"\xd0\xcf")
    run_index(cfg, root)

    (row,) = use(cfg, str(root))["rows"]

    assert (row["name"], row["files"], row["names"], row["text_bytes"]) == ("old", 0, 2, 0)


def test_files_of_an_older_index_are_measured_while_the_indexer_is_idle(tree):
    cfg, root = tree
    before = stored(cfg)
    conn = db.connect(cfg.db_path)
    conn.execute("UPDATE files SET text_bytes = NULL")
    conn.close()
    assert use(cfg, str(root))["unmeasured"] == len(before)

    run_index(cfg, root)

    res = use(cfg, str(root))
    assert res["unmeasured"] == 0 and res["text_bytes"] == sum(before.values())


def test_a_schema_2_index_gets_the_column(tmp_path):
    conn = db.connect(tmp_path / "index.db")
    db.init(conn)
    conn.execute("ALTER TABLE files DROP COLUMN text_bytes")
    conn.execute("UPDATE meta SET value = '2' WHERE key = 'schema_version'")

    db.init(conn)

    assert "text_bytes" in {row[1] for row in conn.execute("PRAGMA table_info(files)")}
    assert db.get_meta(conn, "schema_version") == 3
    conn.close()


def test_the_api_needs_the_token(web, tree):
    _, root = tree

    status, _ = call(web, "/api/usage?path=" + quote(str(root)))
    assert status == 403

    status, body = call(web, "/api/usage?path=" + quote(str(root)), token="secret")
    assert status == 200
    assert {r["name"] for r in json.loads(body)["rows"]} == {"a", "b", "x", "x-y"}
    status, body = call(web, "/api/usage?anywhere=1&limit=3", token="secret")
    assert status == 200 and len(json.loads(body)["rows"]) == 3


def test_the_cli_lists_the_folders(tree, capsys):
    cfg, root = tree

    assert cli.main(["--data-dir", str(cfg.data_dir), "du", str(root)]) == 0

    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.endswith(("\\a", "\\x-y"))]
    assert len(lines) == 2
    assert out.index("\\x-y") < out.index("\\a")  # biggest first: other.txt holds the most text
    assert "(files in " in out
