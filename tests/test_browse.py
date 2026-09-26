"""The folder picker of the Settings page: listing subfolders of this computer."""

import json
import os

import pytest

from sindex import control
from sindex.usn import fixed_drives

from .conftest import call


@pytest.fixture
def tree(tmp_path):
    tmp_path = tmp_path / "tree"  # apart from the fixtures' own folders (the web tests share tmp_path)
    tmp_path.mkdir()
    for name in ("beta", "Alpha", "gamma"):
        (tmp_path / name).mkdir()
    (tmp_path / "Alpha" / "inner").mkdir()
    (tmp_path / "file.txt").write_text("not a folder", encoding="utf-8")
    return tmp_path


def test_only_folders_are_listed_sorted_without_regard_to_case(tree):
    listing = control.list_folders(str(tree))

    assert [f["name"] for f in listing["folders"]] == ["Alpha", "beta", "gamma"]
    assert listing["folders"][0]["path"] == str(tree / "Alpha")
    assert listing["path"] == str(tree) and listing["parent"] == str(tree.parent) and not listing["truncated"]


def test_a_path_is_normalized(tree):
    assert control.list_folders(str(tree / "Alpha" / ".." / "beta"))["path"] == str(tree / "beta")


def test_the_parent_of_a_drive_root_is_the_drive_list():
    drive = fixed_drives()[0]

    listing = control.list_folders(drive)

    assert listing["parent"] == ""
    assert control.list_folders("")["folders"][0]["path"] == drive
    assert control.list_folders("")["parent"] is None


@pytest.mark.parametrize("path", ["relative\\folder", "no-drive"])
def test_a_path_that_is_not_full_is_refused(path):
    with pytest.raises(ValueError, match="not a full path"):
        control.list_folders(path)


def test_a_file_or_a_missing_folder_is_refused_with_a_reason(tree):
    with pytest.raises(ValueError, match="cannot open"):
        control.list_folders(str(tree / "file.txt"))
    with pytest.raises(ValueError, match="cannot open"):
        control.list_folders(str(tree / "nope"))


def test_a_huge_folder_is_cut_and_says_so(tree, monkeypatch):
    monkeypatch.setattr(control, "MAX_FOLDERS", 2)

    listing = control.list_folders(str(tree))

    assert [f["name"] for f in listing["folders"]] == ["Alpha", "beta"] and listing["truncated"]


def test_the_browse_api_needs_the_token(web, tree):
    query = "/api/browse?path=" + str(tree).replace("\\", "%5C")

    assert call(web, query)[0] == 403

    status, body = call(web, query, token="secret")
    assert status == 200
    assert [f["name"] for f in json.loads(body)["folders"]] == ["Alpha", "beta", "gamma"]


def test_the_browse_api_reports_a_bad_path_as_a_400(web, tree):
    status, body = call(web, "/api/browse?path=nope", token="secret")

    assert status == 400 and "not a full path" in json.loads(body)["error"]
    assert os.path.isdir(tree)  # nothing was touched
