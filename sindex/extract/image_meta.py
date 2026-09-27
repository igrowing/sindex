"""Image metadata: EXIF/XMP/IPTC/PNG text chunks, header-only (never decodes pixels).

`Image.open()` and `getexif()` only read the file's header; `.load()`/`.getdata()`/`.convert()` would
decode the full pixel buffer and must never be called here. Camera RAW (cr2/nef/arw/dng) is TIFF-based,
so it is read through the exact same path as a plain TIFF.
"""

from __future__ import annotations

import struct
import xml.etree.ElementTree as ET
from collections.abc import Iterator

from . import Context, ExtractError, MediaFields
from .text import detect_encoding

_XMP_NS = {
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "dc": "http://purl.org/dc/elements/1.1/",
    "photoshop": "http://ns.adobe.com/photoshop/1.0/",
}
_MAX_XMP_BYTES = 1024 * 1024

# IPTC application-record datasets (record 2); see extract/text.py's detect_encoding for charset guessing.
_IPTC_CAPTION = (2, 120)
_IPTC_HEADLINE = (2, 105)
_IPTC_KEYWORDS = (2, 25)
_IPTC_BYLINE = (2, 80)


def _decode_bytes(data: bytes) -> str:
    try:
        return data.decode(detect_encoding(data, complete=True), errors="replace")
    except UnicodeDecodeError:
        return data.decode("latin-1", errors="replace")


def _decode_windows_xp(data: bytes | None) -> str | None:
    if not data:
        return None
    text = data.decode("utf-16-le", errors="replace").rstrip("\x00").strip()
    return text or None


def _decode_user_comment(data: bytes | None) -> str | None:
    """EXIF UserComment: an 8-byte charset code, then the comment (ExifTags.Base.UserComment)."""
    if not data or len(data) <= 8:
        return None
    prefix, body = data[:8], data[8:]
    if prefix.startswith(b"ASCII"):
        text = body.decode("ascii", errors="replace")
    elif prefix.startswith(b"UNICODE"):
        text = body.decode("utf-16", errors="replace")
    elif prefix == b"\x00" * 8:
        return None  # "undefined" charset with no content is common and not an error
    else:
        text = _decode_bytes(body)
    text = text.rstrip("\x00").strip()
    return text or None


def _taken_at(value: str | None) -> str | None:
    """EXIF DateTimeOriginal/DateTime ("YYYY:MM:DD HH:MM:SS") to ISO 8601; unparsable is dropped."""
    if not value:
        return None
    try:
        date, time = value.strip().split(" ", 1)
        y, mo, d = date.split(":")
        return f"{y}-{mo}-{d}T{time}"
    except ValueError:
        return None


def _gps_coord(value: object, ref: object) -> float | None:
    try:
        deg, minutes, seconds = (float(v) for v in value)  # type: ignore[misc]
    except (TypeError, ValueError):
        return None
    coord = deg + minutes / 60 + seconds / 3600
    return -coord if ref in ("S", "W") else coord


def _exif_fields(exif) -> tuple[str | None, str | None, float | None, float | None]:
    from PIL import ExifTags

    make = exif.get(ExifTags.Base.Make.value)
    model = exif.get(ExifTags.Base.Model.value)
    camera = None
    if model and make and not str(model).strip().startswith(str(make).strip()):
        camera = f"{make} {model}".strip()
    else:
        camera = (model or make or "").strip() or None
    taken_at = _taken_at(exif.get(ExifTags.Base.DateTimeOriginal.value) or exif.get(ExifTags.Base.DateTime.value))

    lat = lon = None
    gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
    if gps:
        from PIL.ExifTags import GPS

        lat = _gps_coord(gps.get(GPS.GPSLatitude.value), gps.get(GPS.GPSLatitudeRef.value))
        lon = _gps_coord(gps.get(GPS.GPSLongitude.value), gps.get(GPS.GPSLongitudeRef.value))
    return camera, taken_at, lat, lon


def _exif_text(exif) -> list[str]:
    from PIL import ExifTags

    pieces = []
    if desc := exif.get(ExifTags.Base.ImageDescription.value):
        if isinstance(desc, str) and desc.strip():
            pieces.append(desc.strip())
    if comment := _decode_user_comment(exif.get(ExifTags.Base.UserComment.value)):
        pieces.append(comment)
    for tag in (ExifTags.Base.XPTitle, ExifTags.Base.XPSubject, ExifTags.Base.XPComment, ExifTags.Base.XPKeywords):
        if text := _decode_windows_xp(exif.get(tag.value)):
            pieces.append(text)
    return pieces


def _xmp_text(xmp: bytes | None) -> list[str]:
    if not xmp or len(xmp) > _MAX_XMP_BYTES:
        return []
    try:
        root = ET.fromstring(xmp)  # noqa: S314 -- a small, already-buffered XMP packet, not an external entity risk
    except ET.ParseError:
        return []
    pieces = []
    for desc in root.iter(f"{{{_XMP_NS['rdf']}}}Description"):
        for tag in ("title", "description", "creator"):
            el = desc.find(f"{{{_XMP_NS['dc']}}}{tag}//{{{_XMP_NS['rdf']}}}li")
            if el is not None and el.text and el.text.strip():
                pieces.append(el.text.strip())
        for li in desc.findall(f"{{{_XMP_NS['dc']}}}subject//{{{_XMP_NS['rdf']}}}li"):
            if li.text and li.text.strip():
                pieces.append(li.text.strip())
        headline = desc.find(f"{{{_XMP_NS['photoshop']}}}Headline")
        if headline is not None and headline.text and headline.text.strip():
            pieces.append(headline.text.strip())
    return pieces


def _iptc_text(im) -> list[str]:
    from PIL import IptcImagePlugin

    info = IptcImagePlugin.getiptcinfo(im)
    if not info:
        return []
    pieces = []
    for key in (_IPTC_CAPTION, _IPTC_HEADLINE, _IPTC_BYLINE):
        if (value := info.get(key)) is not None:
            data = value[0] if isinstance(value, list) else value
            if data and (text := _decode_bytes(data).strip()):
                pieces.append(text)
    keywords = info.get(_IPTC_KEYWORDS)
    if keywords is not None:
        values = keywords if isinstance(keywords, list) else [keywords]
        pieces.extend(text for v in values if v and (text := _decode_bytes(v).strip()))
    return pieces


def _png_text(im) -> list[str]:
    # only tEXt/iTXt chunks written before IDAT are visible without calling .load(), which this
    # extractor must never do (it would decode the full pixel buffer).
    pieces = []
    for key in ("Title", "Description", "Author", "Keywords"):
        if (value := im.info.get(key)) and isinstance(value, str) and value.strip():
            pieces.append(value.strip())
    return pieces


def extract(f, ctx: Context) -> Iterator[str]:
    from PIL import Image, UnidentifiedImageError

    try:
        img = Image.open(f)
        width, height = img.size  # header only, forces no pixel decode
        ctx.throttle.checkpoint()

        exif = img.getexif()
        camera, taken_at, lat, lon = _exif_fields(exif)
        pieces = _exif_text(exif)
        ctx.throttle.checkpoint()

        pieces.extend(_xmp_text(img.info.get("xmp")))
        pieces.extend(_iptc_text(img))
        pieces.extend(_png_text(img))
        ctx.throttle.checkpoint()
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, struct.error) as e:
        raise ExtractError(f"image: {type(e).__name__}: {e}") from e

    ctx.media = MediaFields(kind="image", width=width, height=height, taken_at=taken_at, camera=camera,
                             lat=lat, lon=lon)
    text = "\n".join(p for p in pieces if p)
    if text:
        yield text
