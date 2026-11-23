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
from .extract import MEDIA_KINDS, file_ext, kind_of, lists_name_only
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


def queue_target(name: str, cfg: Config, *, force: bool = False) -> tuple[str, int]:
    """(stage, priority) for a file about to be (re)queued: images/videos go to STAGE_META, ahead of the
    slow text formats (PDF, Office, RTF) but behind plain text; everything else goes to STAGE_TEXT."""
    if kind_of(file_ext(name), cfg) in MEDIA_KINDS:
        return db.STAGE_META, db.PRIORITY_FORCED if force else db.PRIORITY_MEDIA
    return db.STAGE_TEXT, queue_priority(name, cfg, force=force)


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
    listed = 0  # names of files whose content is not read that were added or dropped
    stack = [top] if not filt.dir_excluded(top) else []
    conn.execute("BEGIN")
    try:
        while stack:
            d = stack.pop()
            throttle.checkpoint()
            seen: dict[str, tuple[str, int, int]] = {}
            named: dict[str, str] = {}  # files whose content is not read: kept by their name alone
            subdirs: list[str] = []
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
                                subdirs.append(e.path)
                        else:
                            ext = file_ext(e.name)
                            kind = kind_of(ext, cfg)
                            if (kind or lists_name_only(ext, cfg)) and not filt.skip_file_entry(e.path, e.name, attrs):
                                if kind:
                                    seen[e.name.lower()] = (e.name, st.st_size, st.st_mtime_ns)
                                else:
                                    named[e.name.lower()] = e.name
                # the stack pops the last one first: folders are walked in ascending order, so that new ones are
                # numbered in path order and the rows of all of them are appended at the ends of the tables and
                # their indexes, which fills pages instead of splitting them
                subdirs.sort(key=str.lower, reverse=True)
                stack.extend(subdirs)
            except (FileNotFoundError, NotADirectoryError):
                pass  # gone: every file we know here gets queued (and then removed)
            except OSError as e:
                log.debug("cannot list %s: %s", d, e)
                continue  # unreadable is not the same as deleted

            dir_id = db.find_dir(conn, d)
            if dir_id is not None:
                for file_id, name, size, mtime_ns in conn.execute(
                        "SELECT id, name, size, mtime_ns FROM files WHERE dir_id = ?", (dir_id,)).fetchall():
                    entry = seen.pop(name.lower(), None)
                    if name.lower() in named:
                        db.delete_file(conn, file_id)  # its content is not read any more: it stays by name alone
                    elif force or entry is None or entry[1] != size or entry[2] != mtime_ns:
                        stage, prio = queue_target(name, cfg, force=force)
                        db.enqueue(conn, os.path.join(d, name), stage, reason=reason, priority=prio)
                        queued += 1
                for (name,) in conn.execute("SELECT name FROM names WHERE dir_id = ?", (dir_id,)).fetchall():
                    if named.pop(name.lower(), None) is None:
                        db.unlist_name(conn, dir_id, name)  # gone, readable now, or excluded
                        listed += 1
            if named:
                # written here, not queued: there is no text to extract, so a queue row would only double the writes
                if dir_id is None:
                    dir_id = db.add_dir(conn, d)
                for key in sorted(named):  # in key order, appended to the folder's part of the table
                    db.list_name(conn, dir_id, named[key])
                listed += len(named)
            for name, _, _ in seen.values():
                stage, prio = queue_target(name, cfg, force=force)
                db.enqueue(conn, os.path.join(d, name), stage, reason=reason, priority=prio)
                queued += 1

            dirs += 1
            if dirs % COMMIT_EVERY_DIRS == 0:
                conn.execute("COMMIT")
                if progress:
                    progress(d, queued)
                conn.execute("BEGIN")

        # directories we have files for that vanished or became excluded
        stale = conn.execute("SELECT id, path FROM dirs WHERE path = ? OR path LIKE ? ESCAPE '^'",
                             (top, db.like_under(top)))
        for dir_id, d in stale:  # streamed from the cursor, not materialized; `dirs` is not changed meanwhile
            throttle.checkpoint()
            if filt.dir_excluded(d) or not os.path.isdir(d):
                listed += conn.execute("DELETE FROM names WHERE dir_id = ?", (dir_id,)).rowcount
                for (name,) in conn.execute("SELECT name FROM files WHERE dir_id = ?", (dir_id,)).fetchall():
                    stage, prio = queue_target(name, cfg)
                    db.enqueue(conn, os.path.join(d, name), stage, reason="scan", priority=prio)
                    queued += 1
        db.drop_empty_dirs(conn, top)  # the folders of queued files go when the indexer removes their last file
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    log.info("scanned %s: %d dirs, %d files queued, %d names listed", top, dirs, queued, listed)
    return queued
