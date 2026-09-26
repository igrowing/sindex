"""The indexer process: scans, follows the USN journal, extracts and stores text.

Runs as a child of the service and exits with EXIT_RECYCLE after a number of files
(or when its memory grows) so the supervisor can start a fresh process; all work
is persisted in the queue table, so nothing is lost.

Control from the web UI goes through files in the data directory, so the web
side never has to write to the database:
    paused            exists -> indexing is paused
    requests/*.json   {"path": ...} -> reindex a file or folder
    status.json       written by the indexer
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config, control, db, scanner
from .extract import file_ext, kind_of, max_bytes
from .extract.job import Chunk, Result, Task
from .extract.pool import make_runner
from .filters import SKIP_FILE_ATTRS, PathFilter
from .throttle import Stopped, Throttle, apply_process_limits, rss_bytes, stdin_closed
from .usn import (
    REASON_DELETE,
    REASON_RENAME_NEW,
    REASON_RENAME_OLD,
    Change,
    Journal,
    JournalError,
    fixed_drives,
    fs_type,
)

log = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_RECYCLE = 3
DIR_REASONS = REASON_RENAME_OLD | REASON_RENAME_NEW | REASON_DELETE
QUEUE_BATCH = 50  # queue rows looked at per round
PROGRESS_EVERY = 1000  # log a progress line after this many indexed files
STDIN_POLL_SECONDS = 1.0  # how often the supervisor's stdin pipe is checked
MAX_CHUNKS = (1 << db.CHUNK_BITS) - 1  # chunks of one file: the rowid range reserved for it


@dataclass
class Volume:
    letter: str
    roots: list[str]
    fs: str = ""
    journal: Journal | None = None
    journal_id: int | None = None
    next_usn: int | None = None
    last_full_scan: float | None = None
    scanned_roots: list[str] = field(default_factory=list)
    needs_scan: bool = False
    next_poll: float = 0.0


@dataclass(slots=True)
class _Job:
    """A file whose text is being extracted (by the runner) and stored (by us, chunk by chunk)."""

    file_id: int  # also the job id: unique among the files in flight
    path: str
    kind: str
    seq: int = 0  # chunks stored so far


class Indexer:
    def __init__(self, cfg: config.Config, roots: list[str] | None = None,
                 stop: threading.Event | None = None, *, until_idle: bool = False,
                 limits: bool = True) -> None:
        self.cfg = cfg
        self.stop = stop or threading.Event()
        self.until_idle = until_idle
        self.limits = limits
        self.filter = PathFilter(cfg, roots or cfg.roots or fixed_drives())
        self.throttle = Throttle(cfg, self.stop, extra_pause=self._user_pause, on_pause=self._on_pause)
        self.paused_flag = cfg.data_dir / "paused"
        self.requests_dir = cfg.data_dir / "requests"
        self.status_path = cfg.data_dir / "status.json"
        self.volumes: list[Volume] = []
        self.files_done = 0
        self.dirty = False
        self.runner = make_runner(cfg, self.throttle, self.paused_flag)
        self.jobs: dict[int, _Job] = {}  # files being extracted right now, by file id
        self._progress_at = time.monotonic()
        # write batching: one transaction spans many files (see _commit)
        self.in_txn = False
        self.batch_files = 0
        self.batch_chars = 0
        self.batch_started = 0.0
        # stored text plus its index pages take roughly 1.5x the characters in the cache
        self.batch_max_chars = max(1_000_000, cfg.sqlite_cache_mb * 1_000_000 // 2)
        self._status_state = ""
        self._status_at = 0.0
        self.conn: sqlite3.Connection

    # ---- control plane -------------------------------------------------

    def _user_pause(self) -> str | None:
        return "paused by user" if self.paused_flag.exists() else None

    def _on_pause(self, reason: str | None) -> None:
        if reason and not self.jobs:
            self._commit()  # do not sit on uncommitted work while paused
        self._status("paused" if reason else "working", reason or "", force=True)

    # ---- write batching ------------------------------------------------
    #
    # Every commit appends whole 4 KB pages to the WAL and makes FTS5 write a new small
    # segment that is merged and rewritten later, so committing per file multiplies the
    # bytes written (SSD wear). Instead a transaction stays open across many files and
    # is committed when it holds enough work, is old enough, or the indexer goes
    # idle, is paused, recycles or stops. A file's queue row is deleted in the same
    # transaction as its index rows, so a crash loses at most the uncommitted batch,
    # which is simply redone from the queue: never half a file. Files are extracted in
    # parallel, so a commit waits until the files in flight are complete (_settle): a
    # committed half file would be visible to searches until it is finished.
    #
    # Two things must hold for this to save writes (measured, see README):
    #  - no SAVEPOINT per file: FTS5 flushes to a new segment at every savepoint, which
    #    brings the small-segment write amplification straight back. Interrupted files
    #    are instead removed again by _abandon_jobs().
    #  - the batch must fit in SQLite's page cache: otherwise dirty pages spill to the
    #    WAL mid-transaction and hot pages get written over and over. batch_max_chars
    #    is derived from sqlite_cache_mb. It is checked before each new file, so the files
    #    already in flight can overshoot it by their own size.

    def _begin(self) -> None:
        if not self.in_txn:
            self.conn.execute("BEGIN")
            self.in_txn = True
            self.batch_started = time.monotonic()

    def _commit(self) -> None:
        assert not self.jobs, "committing while files are still being extracted"
        if self.in_txn:
            self.conn.execute("COMMIT")
            self.in_txn = False
            self.batch_files = 0
            self.batch_chars = 0

    def _rollback(self) -> None:
        if self.in_txn:
            self.in_txn = False
            self.conn.execute("ROLLBACK")

    def _batch_due(self) -> bool:
        return (self.batch_files + len(self.jobs) >= self.cfg.commit_every_files  # files in flight will join it
                or self.batch_chars >= self.batch_max_chars
                or time.monotonic() - self.batch_started >= self.cfg.commit_every_seconds)

    def _status(self, state: str, detail: str = "", *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and state == self._status_state and now - self._status_at < 10:
            return  # rate-limit per-file updates: status.json is rewritten each time
        self._status_state, self._status_at = state, now
        data = {
            "state": state, "detail": detail, "pid": os.getpid(),
            "rss_mb": round(rss_bytes() / 1048576, 1), "files_done": self.files_done,
            "paused_reason": self.throttle.paused_reason, "roots": self.filter.roots,
            "usn": {v.letter: v.journal is not None for v in self.volumes},
            "updated": time.time(),
        }
        tmp = self.status_path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, self.status_path)
        except (OSError, MemoryError) as e:
            # best effort. At the memory cap even opening a small file can fail, and losing the
            # status file must not turn a normal exit or recycle into a crash
            log.debug("status write failed: %s", e)

    def _take_requests(self) -> None:
        if not self.requests_dir.is_dir():
            return
        for entry in os.scandir(self.requests_dir):
            try:
                req = json.loads(Path(entry.path).read_text(encoding="utf-8"))
                path = os.path.normpath(req["path"])
                stage = db.STAGE_DIR if os.path.isdir(path) else db.STAGE_TEXT
                db.enqueue(self.conn, path, stage, reason="force", priority=db.PRIORITY_FORCED)
                log.info("reindex requested: %s", path)
            except (OSError, ValueError, KeyError, TypeError) as e:
                log.warning("bad request %s: %s", entry.name, e)
            try:
                os.remove(entry.path)
            except OSError as e:
                # the request was already handled; a leftover file only means it is redone once
                log.debug("cannot remove request %s: %s", entry.name, e)

    # ---- main loop -----------------------------------------------------

    def run(self) -> int:
        if self.limits:
            apply_process_limits(self.cfg)
        self.conn = db.connect(self.cfg.db_path, wal_pages=self.cfg.wal_autocheckpoint_pages,
                               cache_mb=self.cfg.sqlite_cache_mb)
        db.init(self.conn)
        log.info("indexer started; roots: %s", ", ".join(self.filter.roots))
        clean = True
        try:
            self._prune_removed_roots()
            self._load_volumes()
            return self._loop()
        except Stopped:
            self._abandon_jobs()
            return EXIT_OK
        except KeyboardInterrupt:
            self._abandon_jobs()
            raise
        except BaseException:
            clean = False  # unexpected (e.g. a database error): do not trust the open batch
            raise
        finally:
            self.runner.close()
            try:
                # after a stop the finished files are consistent (_abandon_jobs() removed the
                # interrupted ones); an unfinished file would just be redone from the queue
                self._commit() if clean else self._rollback()
            except sqlite3.Error:
                log.exception("closing the open batch failed")
            self._status("stopped", force=True)
            for v in self.volumes:
                if v.journal:
                    v.journal.close()
            self.conn.close()

    def _loop(self) -> int:
        while True:
            self.throttle.wait_while_paused()
            self._take_requests()
            did = self._full_scans()
            did |= self._poll_journals()
            did |= self._process_queue()
            if self._should_recycle():
                self._drain()  # the files in flight are complete before the process goes away
                log.info("recycling after %d files (rss %.0f MB)", self.files_done, rss_bytes() / 1048576)
                return EXIT_RECYCLE
            if not did:
                self._commit()
                if self.until_idle:
                    return EXIT_OK
                self._idle()

    def _should_recycle(self) -> bool:
        return (self.files_done >= self.cfg.recycle_after_files
                or rss_bytes() > self.cfg.recycle_rss_mb * 1048576
                or self.runner.wants_recycle)

    def _idle(self) -> None:
        if self.dirty:
            self.dirty = False
            # a small incremental merge keeps the FTS index compact without a big optimize
            self.conn.execute("INSERT INTO chunks(chunks, rank) VALUES ('merge', 64)")
        self._status("idle")
        now = time.monotonic()
        wake = [v.next_poll for v in self.volumes if v.journal and not v.needs_scan]
        delay = min([30.0] + [max(1.0, w - now) for w in wake])
        self.throttle.sleep(delay)

    # ---- volumes, scans and the journal --------------------------------

    def _prune_removed_roots(self) -> None:
        """Forget files under roots that were removed from the configuration."""
        roots = sorted(r.lower() for r in self.filter.roots)
        if db.get_meta(self.conn, "roots") == roots:
            return
        removed = 0
        for (d,) in self.conn.execute("SELECT DISTINCT dir FROM files").fetchall():
            if self.filter.root_of(d) is None:
                self.conn.execute("BEGIN")
                for (fid,) in self.conn.execute("SELECT id FROM files WHERE dir = ?", (d,)).fetchall():
                    db.delete_file(self.conn, fid)
                    removed += 1
                self.conn.execute("COMMIT")
        db.set_meta(self.conn, "roots", roots)
        if removed:
            log.info("removed %d files outside the configured roots", removed)

    def _load_volumes(self) -> None:
        groups: dict[str, list[str]] = {}
        for root in self.filter.roots:
            groups.setdefault(root[:2].upper(), []).append(root)
        now = time.time()
        for letter, roots in groups.items():
            v = Volume(letter, roots, fs=fs_type(letter))
            row = self.conn.execute(
                "SELECT roots, journal_id, next_usn, last_full_scan FROM volumes WHERE letter = ?",
                (letter,)).fetchone()
            if row:
                v.scanned_roots = json.loads(row[0] or "[]")
                v.journal_id, v.next_usn, v.last_full_scan = row[1], row[2], row[3]
            if v.fs == "NTFS":
                try:
                    v.journal = Journal(letter)
                    jid, lowest, _ = v.journal.query()
                    v.needs_scan = v.journal_id != jid or v.next_usn is None or v.next_usn < lowest
                except JournalError as e:
                    log.warning("%s USN journal unavailable (%s); rescanning every %d h",
                                letter, e, self.cfg.rescan_hours)
                    if v.journal:
                        v.journal.close()
                    v.journal = None
            if v.journal is None:
                v.needs_scan = v.last_full_scan is None or now - v.last_full_scan > self.cfg.rescan_hours * 3600
            if sorted(r.lower() for r in roots) != sorted(r.lower() for r in v.scanned_roots):
                v.needs_scan = True
            self.volumes.append(v)

    def _save_volume(self, v: Volume) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO volumes VALUES (?, ?, ?, ?, ?, ?)",
            (v.letter, json.dumps(v.roots), v.fs, v.journal_id, v.next_usn, v.last_full_scan))

    def _full_scans(self) -> bool:
        did = False
        now = time.time()
        for v in self.volumes:
            if v.journal is None and v.last_full_scan and now - v.last_full_scan > self.cfg.rescan_hours * 3600:
                v.needs_scan = True
            if not v.needs_scan:
                continue
            start = None
            if v.journal:
                try:
                    start = v.journal.query()  # position recorded before scanning: nothing is missed
                except JournalError as e:
                    log.warning("%s USN journal unavailable (%s)", v.letter, e)
                    v.journal.close()
                    v.journal = None
            self._settle()  # the scanner runs its own transactions
            for root in v.roots:
                self._status("scanning", root, force=True)
                scanner.scan(self.conn, root, self.filter, self.throttle,
                             progress=lambda d, n: self._status("scanning", d))
            v.last_full_scan = time.time()
            v.scanned_roots = list(v.roots)
            if start:
                v.journal_id, _, v.next_usn = start
            v.needs_scan = False
            self._save_volume(v)
            did = True
        return did

    def _wanted(self, name: str, reason: int, *, is_dir: bool) -> bool:
        if is_dir:
            return bool(reason & DIR_REASONS)
        return kind_of(file_ext(name), self.cfg) is not None

    def _on_change(self, ch: Change) -> None:
        if ch.parent_path is None or self.filter.dir_excluded(ch.parent_path):
            return  # parent gone (its own record covers it) or outside the index
        if ch.is_dir:
            db.enqueue(self.conn, ch.path, db.STAGE_DIR, reason="usn")
        else:
            db.enqueue(self.conn, ch.path, reason="usn")

    def _poll_journals(self) -> bool:
        did = False
        now = time.monotonic()
        for v in self.volumes:
            if v.journal is None or v.needs_scan or now < v.next_poll:
                continue
            v.next_poll = now + self.cfg.usn_poll_seconds
            try:
                jid, lowest, nxt = v.journal.query()
                if jid != v.journal_id or v.next_usn < lowest:
                    log.warning("%s USN journal was reset or wrapped; full rescan", v.letter)
                    v.needs_scan = True
                    continue
                if nxt == v.next_usn:
                    continue
                self._settle()  # new changes and the journal position are stored atomically
                self.conn.execute("BEGIN")
                try:
                    count = 0
                    for ch in v.journal.read(v.next_usn, jid, self._wanted):
                        self._on_change(ch)
                        count += 1
                        if count % 200 == 0:
                            self.throttle.checkpoint()
                    v.next_usn = v.journal.next_usn
                    self._save_volume(v)
                    self.conn.execute("COMMIT")
                except BaseException:
                    self.conn.execute("ROLLBACK")
                    raise
                if count:
                    log.info("%s: %d changes from the USN journal", v.letter, count)
                    did = True
            except JournalError as e:
                if e.rescan:
                    log.warning("%s: %s; full rescan", v.letter, e)
                    v.needs_scan = True
                else:
                    log.warning("%s: %s; falling back to periodic rescans", v.letter, e)
                    v.journal.close()
                    v.journal = None
        return did

    # ---- the queue -------------------------------------------------------

    def _process_queue(self) -> bool:
        # the files already in flight are still queued: ask for that many more rows and skip them
        rows = self.conn.execute("SELECT path, stage, reason FROM queue ORDER BY priority, enqueued_at LIMIT ?",
                                 (QUEUE_BATCH + len(self.jobs),)).fetchall()
        in_flight = {job.path for job in self.jobs.values()}
        did = False
        for path, stage, reason in rows:
            if stage == db.STAGE_TEXT and path in in_flight:
                continue
            did = True
            self.throttle.checkpoint()
            if self.jobs:
                self._pump(0)  # take finished work now: workers block while their output is not read
            if self.in_txn and self._batch_due():
                self._settle()
            if stage == db.STAGE_DIR:
                self._settle()
                self._status("scanning", path, force=True)
                scanner.scan(self.conn, path, self.filter, self.throttle, force=reason == "force")
                self._dequeue(path, stage)
            elif stage == db.STAGE_TEXT:
                self._start_file(path, force=reason == "force")
                if self._should_recycle():
                    break
            else:
                self._dequeue(path, stage)  # stages of later phases
        if not did and self.jobs:
            self._drain()  # nothing new to start: wait for the files in flight
            did = True
        return did

    def _dequeue(self, path: str, stage: str) -> None:
        self.conn.execute("DELETE FROM queue WHERE path = ? AND stage = ?", (path, stage))

    # ---- extraction pipeline -----------------------------------------------
    #
    # A queued file is prepared here (stat, what to do with it, its `files` row), handed to the
    # runner, and its chunks are stored as they come back. Up to `extract_workers` files are in
    # flight, so the runner (not this process) is where the parsing time is spent.

    def _start_file(self, path: str, *, force: bool) -> None:
        self._status("indexing", path)
        self._begin()
        extracted, job = self._prepare_file(path, force=force)
        if job is None:
            self._file_done(path, extracted=extracted)
            return
        # tracked from here on, even while it waits for a free worker: its `files` row exists, so a
        # stop must remove it and nothing may commit it half done
        self.jobs[job.file_id] = job
        while not self.runner.has_slot():
            self.throttle.checkpoint()
            self._pump(1.0)
        self.runner.submit(Task(job.file_id, path, job.kind, MAX_CHUNKS))

    def _pump(self, timeout: float) -> None:
        for msg in self.runner.poll(timeout):
            self._on_message(msg)

    def _drain(self) -> None:
        """Wait until every file in flight is complete."""
        while self.jobs:
            self.throttle.checkpoint()
            self._pump(1.0)

    def _settle(self) -> None:
        """Commit at a moment when no file is half done."""
        self._drain()
        self._commit()

    def _abandon_jobs(self) -> None:
        # interrupted: remove the half-indexed files; their queue rows are still there, so they are
        # redone later. (No savepoint: see the note on write batching.)
        for file_id in self.jobs:
            db.delete_file(self.conn, file_id)
        self.jobs.clear()

    def _on_message(self, msg: Chunk | Result) -> None:
        job = self.jobs[msg.job]
        if isinstance(msg, Chunk):
            self.conn.execute("INSERT INTO chunks (rowid, body, source) VALUES (?, ?, 'text')",
                              ((job.file_id << db.CHUNK_BITS) + job.seq, msg.text))
            job.seq += 1
            self.batch_chars += len(msg.text)
            return
        del self.jobs[msg.job]
        self.conn.execute("UPDATE files SET status = ?, encoding = ?, error = ? WHERE id = ?",
                          (msg.status, msg.encoding, msg.error, job.file_id))
        self._file_done(job.path, extracted=True)

    def _file_done(self, path: str, *, extracted: bool) -> None:
        self._dequeue(path, db.STAGE_TEXT)
        self.dirty = True
        self.batch_files += 1
        if extracted:
            self.files_done += 1
            if self.files_done % PROGRESS_EVERY == 0:
                self._log_progress()

    def _log_progress(self) -> None:
        stats = control.index_stats(self.conn, self.cfg)
        now = time.monotonic()
        rate = PROGRESS_EVERY / max(now - self._progress_at, 0.001)
        self._progress_at = now
        log.info("progress: %d files indexed, %d files in queue (%.1f files/s)",
                 stats["total_files"], stats["queued"], rate)

    def _prepare_file(self, path: str, *, force: bool) -> tuple[bool, _Job | None]:
        """Decide what to do with a queued file and record it as being indexed. Runs inside a transaction.

        Returns whether the file's content is read, and the job that still has to extract its text
        (None when the file was dropped, is unchanged, or is too big).
        """
        conn = self.conn
        d, name = os.path.split(path)
        row = conn.execute("SELECT id, size, mtime_ns, status FROM files WHERE dir = ? AND name = ?",
                           (d, name)).fetchone()
        kind = self.filter.file_kind(path)
        try:
            st = os.stat(path, follow_symlinks=False) if kind else None
        except (FileNotFoundError, NotADirectoryError):
            st = None
        except OSError as e:
            log.debug("cannot stat %s: %s", path, e)
            return False, None  # transient (locked, access denied): keep what we have
        if st is None or not os.path.isfile(path) or st.st_file_attributes & SKIP_FILE_ATTRS:
            if row:
                db.delete_file(conn, row[0])
            return False, None
        if row and not force and row[1] == st.st_size and row[2] == st.st_mtime_ns and row[3] != "indexing":
            return False, None  # unchanged ('indexing' is a leftover of an interrupted run: redo)

        file_id = conn.execute(
            """INSERT INTO files (dir, name, size, mtime_ns, status, indexed_at)
               VALUES (?, ?, ?, ?, 'indexing', ?)
               ON CONFLICT (dir, name) DO UPDATE SET
                 name = excluded.name, size = excluded.size, mtime_ns = excluded.mtime_ns,
                 status = 'indexing', encoding = NULL, error = NULL, indexed_at = excluded.indexed_at
               RETURNING id""",
            (d, name, st.st_size, st.st_mtime_ns, time.time())).fetchone()[0]
        db.delete_chunks(conn, file_id)
        if st.st_size > max_bytes(kind, self.cfg):
            conn.execute("UPDATE files SET status = 'too_big' WHERE id = ?", (file_id,))
            return True, None
        return True, _Job(file_id, path, kind)


def _stop_on_stdin_eof(stop: threading.Event) -> None:
    def watch() -> None:
        # polls, see stdin_closed(). Daemon: it holds nothing, and it ends by itself once `stop` is set
        while not stop.wait(STDIN_POLL_SECONDS):
            if stdin_closed():
                stop.set()
    threading.Thread(target=watch, daemon=True, name="stdin-watch").start()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="sindex.indexer")
    ap.add_argument("--data-dir", type=Path)
    ap.add_argument("--root", action="append", help="index this folder (repeatable; overrides config)")
    ap.add_argument("--until-idle", action="store_true", help="exit when there is nothing left to do")
    ap.add_argument("--no-limits", action="store_true", help="do not apply priority/job limits")
    ap.add_argument("--stdin-stop", action="store_true", help="stop when stdin is closed (supervisor)")
    ap.add_argument("--console", action="store_true", help="also log to the console")
    args = ap.parse_args(argv)

    cfg = config.load(args.data_dir)
    config.setup_logging(cfg.data_dir, "indexer", console=args.console)
    stop = threading.Event()
    if args.stdin_stop:
        _stop_on_stdin_eof(stop)
    indexer = Indexer(cfg, args.root, stop, until_idle=args.until_idle, limits=not args.no_limits)
    try:
        code = indexer.run()
    except KeyboardInterrupt:
        code = EXIT_OK
    return code


if __name__ == "__main__":
    sys.exit(main())
