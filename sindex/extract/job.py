"""One extraction job: a file goes in, normalized text chunks and a final status come out.

The same generator runs inside the indexer (`extract_workers = 0`) or inside an extraction
worker process (pool.py), so the indexer sees identical messages either way.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass

from .. import textnorm
from ..config import Config
from ..throttle import Stopped, Throttle
from . import Context, ExtractError, NotText, extract

log = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class Task:
    job: int  # chosen by the caller; unique among the jobs in flight
    path: str
    kind: str
    max_chunks: int  # the storage limit for one file; the rest of the text is dropped


@dataclass(slots=True, frozen=True)
class Chunk:
    job: int
    text: str  # already through textnorm.normalize()


@dataclass(slots=True, frozen=True)
class Result:
    """Always the last message of a job. `error` is set exactly when `status` is 'error'."""

    job: int
    status: str  # ok | truncated | empty | binary | error
    error: str | None = None
    encoding: str | None = None
    recycle: bool = False  # this process should be replaced before it takes another file


def run_job(task: Task, cfg: Config, throttle: Throttle) -> Iterator[Chunk | Result]:
    """Extract one file. Only Stopped and KeyboardInterrupt escape; every other failure is a Result."""
    ctx = Context(throttle)
    info: dict = {}
    seq = 0
    status, error, recycle = "ok", None, False
    pieces = None
    try:
        pieces = extract(task.path, task.kind, ctx)
        for text in textnorm.chunks(pieces, cfg.chunk_chars, cfg.max_chars_per_file, info):
            yield Chunk(task.job, textnorm.normalize(text))
            seq += 1
            if seq >= task.max_chunks:
                info["truncated"] = True
                break
        if seq == 0:
            status = "empty"
        elif info.get("truncated"):
            status = "truncated"
    except NotText:
        status = "binary"
    except ExtractError as e:
        status, error = "error", str(e)[:500]
    except OSError as e:
        status, error = "error", f"{type(e).__name__}: {e}"[:500]
    except MemoryError:
        status, error, recycle = "error", "out of memory", True
    except (Stopped, KeyboardInterrupt):
        raise
    except Exception as e:  # a parser bug must not stop the indexer
        log.exception("unexpected error extracting %s", task.path)
        status, error = "error", f"{type(e).__name__}: {e}"[:500]
    finally:
        if pieces is not None:
            pieces.close()
    yield Result(task.job, status, error, ctx.encoding, recycle)
