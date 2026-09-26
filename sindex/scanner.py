"""Walks a directory tree and queues files that are new, changed or gone.

Compares one directory at a time against the `files` table, so memory use is
bounded by the largest directory, not by the size of the tree.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Callable

from . import db
from .config import Config
from .extract import file_ext, kind_of
from .filters import A_DIRECTORY, PathFilter
from .throttle import Throttle

log = logging.getLogger(__name__)

COMMIT_EVERY_DIRS = 200


def queue_priority(name: str, cfg: Config, *, force: bool = False) -> int:
    """Priority for a file about to be queued: a forced reindex goes first; otherwise plain text and
    markup (cheap to parse) go before the slower formats (PDF, Office, RTF), so their content is
    searchable while the slow formats are still being read."""
    if force:
        return db.PRIORITY_FORCED
    return db.PRIORITY_TEXT_EASY if kind_of(file_ext(name), cfg) == "text" else db.PRIORITY_TEXT_HARD


def scan(conn: sqlite3.Connection, top: str, filt: PathFilter, throttle: Throttle,
         *, progress: Callable[[str, int], None] | None = None, force: bool = False) -> int:
    """Reconcile the subtree `top` with the index. Returns the number of files queued.

    `force` queues every file for re-extraction, changed or not.
    Manages its own transactions; must be called outside one.
    """
    cfg = filt.cfg
    reason = "force" if force else "scan"
    queued = 0
    dirs = 0
    stack = [top] if not filt.dir_excluded(top) else []
    conn.execute("BEGIN")
    try:
        while stack:
            d = stack.pop()
            throttle.checkpoint()
            seen: dict[str, tuple[str, int, int]] = {}
            try:
                with os.scandir(d) as it:
                    for e in it:
                        try:
                            st = e.stat(follow_symlinks=False)  # served from the directory listing
                        except OSError:
                            continue
                        attrs = st.st_file_attributes
                        if attrs & A_DIRECTORY:
                            if not filt.skip_dir_entry(e.path, e.name, attrs):
                                stack.append(e.path)
                        elif kind_of(file_ext(e.name), cfg) and not filt.skip_file_entry(e.path, e.name, attrs):
                            seen[e.name.lower()] = (e.name, st.st_size, st.st_mtime_ns)
            except (FileNotFoundError, NotADirectoryError):
                pass  # gone: every file we know here gets queued (and then removed)
            except OSError as e:
                log.debug("cannot list %s: %s", d, e)
                continue  # unreadable is not the same as deleted

            for name, size, mtime_ns in conn.execute(
                    "SELECT name, size, mtime_ns FROM files WHERE dir = ?", (d,)).fetchall():
                entry = seen.pop(name.lower(), None)
                if force or entry is None or entry[1] != size or entry[2] != mtime_ns:
                    prio = queue_priority(name, cfg, force=force)
                    db.enqueue(conn, os.path.join(d, name), reason=reason, priority=prio)
                    queued += 1
            for name, _, _ in seen.values():
                prio = queue_priority(name, cfg, force=force)
                db.enqueue(conn, os.path.join(d, name), reason=reason, priority=prio)
                queued += 1

            dirs += 1
            if dirs % COMMIT_EVERY_DIRS == 0:
                conn.execute("COMMIT")
                if progress:
                    progress(d, queued)
                conn.execute("BEGIN")

        # directories we have files for that vanished or became excluded
        stale = conn.execute(
            "SELECT DISTINCT dir FROM files WHERE dir = ? OR dir LIKE ? ESCAPE '^'",
            (top, db.like_under(top)),
        )
        for (d,) in stale:  # streamed from the cursor, not materialized
            throttle.checkpoint()
            if filt.dir_excluded(d) or not os.path.isdir(d):
                for (name,) in conn.execute("SELECT name FROM files WHERE dir = ?", (d,)).fetchall():
                    db.enqueue(conn, os.path.join(d, name), reason="scan", priority=queue_priority(name, cfg))
                    queued += 1
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    log.info("scanned %s: %d dirs, %d files queued", top, dirs, queued)
    return queued
