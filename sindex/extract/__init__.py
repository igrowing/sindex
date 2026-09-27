"""Text extraction dispatch. Every extractor is a generator of text pieces."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from ..config import IMAGE_EXTS, MARKUP_EXTS, OFFICE_EXTS, PDF_EXTS, RTF_EXTS, VIDEO_EXTS, Config
from ..throttle import Throttle, ThrottledReader

MEDIA_KINDS = frozenset({"image", "video"})


class ExtractError(Exception):
    """The file could not be parsed (status 'error')."""


class NotText(Exception):
    """The file does not contain text after all (status 'binary')."""


@dataclass(slots=True, frozen=True)
class MediaFields:
    """Structured fields read from an image/video header (Phase 2); written to the `media` table."""

    kind: str  # "image" | "video"
    width: int | None = None
    height: int | None = None
    duration_s: float | None = None
    taken_at: str | None = None  # "YYYY-MM-DDTHH:MM:SS", camera/device local time
    camera: str | None = None
    lat: float | None = None
    lon: float | None = None
    codec: str | None = None


class Context:
    def __init__(self, throttle: Throttle) -> None:
        self.throttle = throttle
        self.encoding: str | None = None  # set by extractors that detect one
        self.media: MediaFields | None = None  # set by image/video extractors on a successful header parse


def file_ext(path: str) -> str:
    return os.path.splitext(path)[1].lower().lstrip(".")


def kind_of(ext: str, cfg: Config) -> str | None:
    if cfg.exclude_extensions and cfg.ext_excluded(ext):
        return None
    if ext in PDF_EXTS:
        return "pdf"
    if ext in RTF_EXTS:
        return "rtf"
    if ext in OFFICE_EXTS:
        return "odf" if ext.startswith("o") else "ooxml"
    if cfg.index_media_metadata:
        if ext in IMAGE_EXTS:
            return "image"
        if ext in VIDEO_EXTS:
            return "video"
    if ext in cfg.text_exts:
        return "text"
    return None


def max_bytes(kind: str, cfg: Config) -> int:
    mb = {"pdf": cfg.max_pdf_mb, "ooxml": cfg.max_office_mb, "odf": cfg.max_office_mb,
          "image": cfg.max_media_mb, "video": cfg.max_media_mb}.get(kind, cfg.max_text_mb)
    return mb * 1024 * 1024


@contextmanager
def _open(path: str, ctx: Context):
    with open(path, "rb", buffering=0) as raw:
        yield ThrottledReader(raw, ctx.throttle)


def extract(path: str, kind: str, ctx: Context) -> Iterator[str]:
    from . import image_meta, office, pdf, rtf, text, video_meta

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
        elif kind == "image":
            yield from image_meta.extract(f, ctx)
        elif kind == "video":
            yield from video_meta.extract(f, ctx)
        else:
            raise ValueError(kind)
