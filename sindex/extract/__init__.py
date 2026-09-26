"""Text extraction dispatch. Every extractor is a generator of text pieces."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from ..config import MARKUP_EXTS, OFFICE_EXTS, PDF_EXTS, RTF_EXTS, Config
from ..throttle import Throttle, ThrottledReader


class ExtractError(Exception):
    """The file could not be parsed (status 'error')."""


class NotText(Exception):
    """The file does not contain text after all (status 'binary')."""


class Context:
    def __init__(self, throttle: Throttle) -> None:
        self.throttle = throttle
        self.encoding: str | None = None  # set by extractors that detect one


def file_ext(path: str) -> str:
    return os.path.splitext(path)[1].lower().lstrip(".")


def kind_of(ext: str, cfg: Config) -> str | None:
    if cfg.exclude_extensions and ext in cfg.excluded_exts:
        return None
    if ext in PDF_EXTS:
        return "pdf"
    if ext in RTF_EXTS:
        return "rtf"
    if ext in OFFICE_EXTS:
        return "odf" if ext.startswith("o") else "ooxml"
    if ext in cfg.text_exts:
        return "text"
    return None


def max_bytes(kind: str, cfg: Config) -> int:
    mb = {"pdf": cfg.max_pdf_mb, "ooxml": cfg.max_office_mb, "odf": cfg.max_office_mb}.get(kind, cfg.max_text_mb)
    return mb * 1024 * 1024


@contextmanager
def _open(path: str, ctx: Context):
    with open(path, "rb", buffering=0) as raw:
        yield ThrottledReader(raw, ctx.throttle)


def extract(path: str, kind: str, ctx: Context) -> Iterator[str]:
    from . import office, pdf, rtf, text

    with _open(path, ctx) as f:
        if kind == "text":
            yield from text.extract(f, ctx, html=file_ext(path) in MARKUP_EXTS)
        elif kind == "ooxml":
            yield from office.extract_ooxml(f, ctx, file_ext(path))
        elif kind == "odf":
            yield from office.extract_odf(f, ctx)
        elif kind == "rtf":
            yield from rtf.extract(f, ctx)
        elif kind == "pdf":
            yield from pdf.extract(f, ctx)
        else:
            raise ValueError(kind)
