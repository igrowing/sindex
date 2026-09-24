"""Plain text: encoding detection on a bounded sample, then streaming decode."""

from __future__ import annotations

import codecs
import re
from collections.abc import Iterator
from html.parser import HTMLParser

import charset_normalizer

from . import Context, NotText

SAMPLE_BYTES = 64 * 1024
BLOCK_BYTES = 64 * 1024

_BOMS = [
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
]
_DECLARED = re.compile(
    rb"""<\?xml[^>]*encoding=["']([\w.:-]+)|<meta[^>]*charset=["']?([\w.:-]+)""", re.IGNORECASE)
_CONTROL = re.compile(rb"[\x00-\x08\x0e-\x1a\x1c-\x1f]")


def _valid_utf8(sample: bytes, *, complete: bool) -> bool:
    try:
        sample.decode("utf-8")
        return True
    except UnicodeDecodeError as e:
        # a multi-byte sequence cut off by the end of the sample is fine
        return not complete and e.start >= len(sample) - 3 and e.reason == "unexpected end of data"


def _decodes(sample: bytes, encoding: str, *, complete: bool) -> bool:
    try:
        dec = codecs.getincrementaldecoder(encoding)("strict")
        dec.decode(sample, final=complete)
        return True
    except (UnicodeDecodeError, LookupError):
        return False


def _utf16_without_bom(sample: bytes) -> str | None:
    n = min(len(sample), 4096) // 2 * 2
    if n < 16:
        return None
    even_zero = sample[0:n:2].count(0) / (n / 2)
    odd_zero = sample[1:n:2].count(0) / (n / 2)
    if odd_zero > 0.3 and even_zero < 0.05:
        return "utf-16-le"
    if even_zero > 0.3 and odd_zero < 0.05:
        return "utf-16-be"
    return None


def detect_encoding(sample: bytes, *, complete: bool) -> str:
    """Pick a codec name for `sample`; raises NotText for binary data."""
    for bom, name in _BOMS:
        if sample.startswith(bom):
            return name
    if enc := _utf16_without_bom(sample):
        return enc
    if b"\x00" in sample or len(_CONTROL.findall(sample[:8192])) > len(sample[:8192]) * 0.05:
        raise NotText
    if m := _DECLARED.search(sample[:2048]):
        declared = (m.group(1) or m.group(2)).decode("ascii").lower()
        try:
            name = codecs.lookup(declared).name
        except LookupError:
            name = None
        if name and _decodes(sample, name, complete=complete):
            return name
    if _valid_utf8(sample, complete=complete):
        return "utf-8"
    best = charset_normalizer.from_bytes(sample[:SAMPLE_BYTES]).best()
    if best is not None and best.encoding:
        return best.encoding
    return "cp1252"


class _MarkupText(HTMLParser):
    """Collects character data from HTML/XML, skipping script and style."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in ("p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"):
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.out.append(data)

    def take(self) -> str:
        text = "".join(self.out)
        self.out.clear()
        return text


def extract(f, ctx: Context, *, html: bool = False) -> Iterator[str]:
    sample = f.read(SAMPLE_BYTES)
    if not sample:
        return
    complete = len(sample) < SAMPLE_BYTES
    encoding = detect_encoding(sample, complete=complete)
    ctx.encoding = encoding
    decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
    parser = _MarkupText() if html else None

    block = sample
    while True:
        final = not block
        text = decoder.decode(block, final=final)
        if parser is not None:
            parser.feed(text)
            if final:
                parser.close()
            text = parser.take()
        if text:
            yield text
        if final:
            return
        block = f.read(BLOCK_BYTES)
