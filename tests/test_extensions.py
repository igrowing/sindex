"""Which files are indexed, decided by extension."""

import pytest

from sindex.config import MARKUP_EXTS
from sindex.extract import extract, kind_of

from .test_index_search import names, run_index

REQUESTED = "txt csv json yaml yml toml xml htm html xhtml md ini".split()
ALSO_TEXT = ("log tsv jsonl jsonc geojson rst tex srt ass reg inf ics vcf diff patch sln py ino hs sql "
             "svg rss gpx kml xaml fb2 plist csproj props resx").split()
NOT_TEXT = "png jpg exe dll zip mp4 bin iso db sqlite pyc".split()


@pytest.mark.parametrize("ext", REQUESTED + ALSO_TEXT)
def test_indexed_as_text(cfg, ext):
    assert kind_of(ext, cfg) == "text"


@pytest.mark.parametrize("ext", NOT_TEXT)
def test_binary_formats_are_not_indexed(cfg, ext):
    assert kind_of(ext, cfg) is None


def test_markup_extensions_are_all_indexed(cfg):
    assert MARKUP_EXTS <= cfg.text_exts, "an extension that gets tag stripping but is never indexed is dead"


def test_extra_extensions_from_config(cfg):
    assert kind_of("weird", cfg) is None
    cfg.extra_text_extensions = [".weird", "Other"]
    assert kind_of("weird", cfg) == "text" and kind_of("other", cfg) == "text"


@pytest.mark.parametrize("ext", ["svg", "xaml", "fb2", "csproj", "rss"])
def test_markup_family_has_tags_stripped(tmp_path, ctx, ext):
    p = tmp_path / f"doc.{ext}"
    p.write_text('<?xml version="1.0"?><root><title>Hello world</title><script>hidden()</script></root>',
                 encoding="utf-8")
    # whether tags are stripped is decided from the file extension
    out = "".join(extract(str(p), "text", ctx))
    assert "Hello world" in out
    assert "<title>" not in out
    assert "hidden" not in out


def test_requested_file_types_are_found_by_search(cfg, tmp_path):
    root = tmp_path / "mixed"
    root.mkdir()
    # file name -> (content, a word that only that file contains)
    files = {
        "notes.txt": ("plain zebrafish text", "zebrafish"),
        "table.csv": ("id,name\n1,csvpelican\n", "csvpelican"),
        "conf.json": ('{"key": "jsonwalrus"}', "jsonwalrus"),
        "conf.yaml": ("key: yamlmoose\n", "yamlmoose"),
        "conf.yml": ("key: ymlbadger\n", "ymlbadger"),
        "conf.toml": ('key = "tomlheron"\n', "tomlheron"),
        "data.xml": ("<root><item>xmlotter</item><script>ignored()</script></root>", "xmlotter"),
        "page.htm": ("<html><body><p>htmfalcon</p></body></html>", "htmfalcon"),
        "page.html": ("<html><body><p>htmllynx</p></body></html>", "htmllynx"),
        "page.xhtml": ("<html><body><p>xhtmlmarten</p></body></html>", "xhtmlmarten"),
        "readme.md": ("# Title\n\nmarkdownibex\n", "markdownibex"),
        "settings.ini": ("[section]\nkey = inicrane\n", "inicrane"),
    }
    for name, (text, _) in files.items():
        (root / name).write_text(text, encoding="utf-8")
    # .reg exports are UTF-16 with a BOM
    reg = "Windows Registry Editor Version 5.00\r\n\r\n[HKEY_CURRENT_USER\\regheron]\r\n"
    (root / "export.reg").write_text(reg, encoding="utf-16")
    run_index(cfg, root)

    for name, (_, word) in files.items():
        assert names(cfg, word) == {name}, name
    assert names(cfg, "regheron") == {"export.reg"}
    assert names(cfg, "ignored") == set(), "script contents are not text"
