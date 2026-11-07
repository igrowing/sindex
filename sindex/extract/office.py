"""OOXML (docx/xlsx/pptx) and OpenDocument (odt/ods/odp) via zipfile + streaming expat."""

from __future__ import annotations

import zipfile
from collections.abc import Iterable, Iterator
from xml.parsers import expat

import regex

from . import Context, ExtractError

BLOCK_BYTES = 64 * 1024
MAX_UNCOMPRESSED = 512 * 1024 * 1024  # zip bomb guard for the members we read
MAX_RATIO = 200


class _XmlText:
    """Streams character data out of an XML part without building a tree.

    text_tags: local names whose character data is text (None = all data inside `body_tag`).
    """

    def __init__(self, text_tags: frozenset[str] | None, para_tags: frozenset[str],
                 tab_tags: frozenset[str] = frozenset(), break_tags: frozenset[str] = frozenset(),
                 space_tags: frozenset[str] = frozenset(), body_tag: str | None = None) -> None:
        self.text_tags = text_tags
        self.para_tags = para_tags
        self.tab_tags = tab_tags
        self.break_tags = break_tags
        self.space_tags = space_tags
        self.body_tag = body_tag
        self.out: list[str] = []
        self._in_text = 0
        self._in_body = body_tag is None

        p = expat.ParserCreate(namespace_separator=" ")
        p.StartElementHandler = self._start
        p.EndElementHandler = self._end
        p.CharacterDataHandler = self._data
        p.StartDoctypeDeclHandler = self._doctype
        p.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
        self.parser = p

    @staticmethod
    def _doctype(*_):
        raise ExtractError("DOCTYPE not allowed in office XML")

    def _start(self, name: str, attrs) -> None:
        local = name.rpartition(" ")[2]
        if local == self.body_tag:
            self._in_body = True
        if self.text_tags is not None and local in self.text_tags:
            self._in_text += 1
        elif local in self.tab_tags:
            self.out.append("\t")
        elif local in self.break_tags:
            self.out.append("\n")
        elif local in self.space_tags:
            count = next((v for k, v in attrs.items() if k.endswith(" c")), "1")
            self.out.append(" " * min(int(count) if count.isdigit() else 1, 100))

    def _end(self, name: str) -> None:
        local = name.rpartition(" ")[2]
        if self.text_tags is not None and local in self.text_tags:
            self._in_text -= 1
        if local in self.para_tags:
            self.out.append("\n")

    def _data(self, data: str) -> None:
        if self._in_body and (self.text_tags is None or self._in_text > 0):
            self.out.append(data)

    def run(self, stream, ctx: Context) -> Iterator[str]:
        try:
            while block := stream.read(BLOCK_BYTES):
                ctx.throttle.consume(0)
                self.parser.Parse(block, False)
                if self.out:
                    yield "".join(self.out)
                    self.out.clear()
            self.parser.Parse(b"", True)
        except expat.ExpatError as e:
            raise ExtractError(f"XML: {e}") from e
        if self.out:
            yield "".join(self.out)


def _open_zip(f) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(f)
    except (zipfile.BadZipFile, OSError) as e:
        raise ExtractError(f"zip: {e}") from e


def _check_members(zf: zipfile.ZipFile, names: Iterable[str]) -> list[zipfile.ZipInfo]:
    infos = [zf.getinfo(n) for n in names]
    if sum(i.file_size for i in infos) > MAX_UNCOMPRESSED:
        raise ExtractError("zip: uncompressed content too large")
    for i in infos:
        if i.compress_size and i.file_size / i.compress_size > MAX_RATIO and i.file_size > 10 * 1024 * 1024:
            raise ExtractError(f"zip: suspicious compression ratio in {i.filename}")
    return infos


def _numbered(names: Iterable[str], pattern: str) -> list[str]:
    rx = regex.compile(pattern)
    found = [(int(m.group(1)), n) for n in names if (m := rx.fullmatch(n))]
    return [n for _, n in sorted(found)]


_W_TEXT = {"text_tags": frozenset({"t"}), "para_tags": frozenset({"p", "si"}),
           "tab_tags": frozenset({"tab"}), "break_tags": frozenset({"br", "cr"})}


def extract_ooxml(f, ctx: Context, ext: str) -> Iterator[str]:
    zf = _open_zip(f)
    with zf:
        names = zf.namelist()
        if ext.startswith("doc"):
            parts = ["word/document.xml"]
            parts += _numbered(names, r"word/header(\d+)\.xml") + _numbered(names, r"word/footer(\d+)\.xml")
            parts += [n for n in ("word/footnotes.xml", "word/endnotes.xml", "word/comments.xml") if n in names]
        elif ext.startswith("xl"):
            # cell text lives in the shared string table; numbers are not worth indexing
            parts = [n for n in ("xl/sharedStrings.xml",) if n in names]
        else:
            parts = _numbered(names, r"ppt/slides/slide(\d+)\.xml")
            parts += _numbered(names, r"ppt/notesSlides/notesSlide(\d+)\.xml")
        parts = [p for p in parts if p in names]
        if not parts:
            raise ExtractError("no text parts found")
        for info in _check_members(zf, parts):
            with zf.open(info) as stream:
                yield from _XmlText(**_W_TEXT).run(stream, ctx)


def extract_odf(f, ctx: Context) -> Iterator[str]:
    zf = _open_zip(f)
    with zf:
        if "content.xml" not in zf.namelist():
            raise ExtractError("no content.xml")
        for info in _check_members(zf, ["content.xml"]):
            with zf.open(info) as stream:
                yield from _XmlText(
                    text_tags=None,
                    para_tags=frozenset({"p", "h", "list-item", "table-cell"}),
                    tab_tags=frozenset({"tab"}),
                    break_tags=frozenset({"line-break"}),
                    space_tags=frozenset({"s"}),
                    body_tag="body",
                ).run(stream, ctx)
