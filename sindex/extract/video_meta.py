"""Video metadata: container tags via pymediainfo, header-only (never decodes frames).

`MediaInfo.parse(..., parse_speed=0.3)` only scans the container's index/header structures; it never
decodes a video frame. `parse_speed` must not be raised to get "more" out of a file — that trades away
the header-only guarantee this extractor exists to keep.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator

from . import Context, ExtractError, MediaFields

log = logging.getLogger(__name__)

PARSE_SPEED = 0.3

# QuickTime ©xyz / ISO 6709: "+40.6892-074.0445/" (lat, lon, optional altitude, trailing slash).
_ISO6709 = re.compile(r"^([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)")

_missing_library_logged = False


def _taken_at(value: str | None) -> str | None:
    """"YYYY-MM-DD HH:MM:SS" (optionally with a trailing zone) to ISO 8601; unparsable is dropped."""
    if not value:
        return None
    m = re.match(r"(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})", value.strip())
    if not m:
        return None
    y, mo, d, h, mi, s = m.groups()
    return f"{y}-{mo}-{d}T{h}:{mi}:{s}"


def _gps(xyz: str | None) -> tuple[float | None, float | None]:
    if not xyz:
        return None, None
    m = _ISO6709.match(xyz.strip())
    if not m:
        return None, None
    return float(m.group(1)), float(m.group(2))


def extract(f, ctx: Context) -> Iterator[str]:
    global _missing_library_logged
    from pymediainfo import MediaInfo

    if not MediaInfo.can_parse():
        if not _missing_library_logged:
            log.warning("video metadata: MediaInfo library (MediaInfo.dll) not available")
            _missing_library_logged = True
        raise ExtractError("video: MediaInfo library not available")

    ctx.throttle.checkpoint()
    try:
        info = MediaInfo.parse(f, parse_speed=PARSE_SPEED)
    except (OSError, RuntimeError, ValueError) as e:
        raise ExtractError(f"video: {type(e).__name__}: {e}") from e
    ctx.throttle.checkpoint()

    if not info.general_tracks:
        raise ExtractError("video: no readable container header")
    general = info.general_tracks[0]
    video = info.video_tracks[0] if info.video_tracks else None

    duration_ms = general.duration or (video.duration if video else None)
    duration_s = float(duration_ms) / 1000 if duration_ms else None
    width = int(video.width) if video and video.width else None
    height = int(video.height) if video and video.height else None
    codec = (video.codec_id or video.format) if video else None
    taken_at = _taken_at(general.encoded_date or general.recorded_date or general.tagged_date)
    camera = general.performer or None
    lat, lon = _gps(general.xyz)

    ctx.media = MediaFields(kind="video", width=width, height=height, duration_s=duration_s,
                             taken_at=taken_at, camera=camera, lat=lat, lon=lon, codec=codec)

    pieces = [p.strip() for p in (general.title or general.movie_name, general.comment, general.performer)
              if p and p.strip()]
    text = "\n".join(dict.fromkeys(pieces))  # de-dupe while keeping order (title and movie_name often match)
    if text:
        yield text
