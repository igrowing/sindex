"""Running extraction jobs: in the indexer itself, or in a pool of worker processes.

Parsing (PDFs above all) is where the CPU time goes, and a Python process can use one core at most,
so `extract_workers` processes parse files in parallel and stream the normalized chunks back over
pipes. The indexer stays the only writer of the database. Both runners have the same interface:

    has_slot()         a job can be submitted now
    submit(task)       start a job
    poll(timeout)      messages (Chunk, then a final Result per job) that are ready
    close()            stop everything
    wants_recycle      the indexer process itself should be replaced (inline runner after MemoryError)

Everything is bounded: a worker blocks on `send` while the indexer is not reading its pipe, so no
more than a chunk or two per worker is in flight.
"""

from __future__ import annotations

import dataclasses
import multiprocessing
import time
from dataclasses import dataclass
from multiprocessing.connection import Connection, wait
from multiprocessing.process import BaseProcess
from pathlib import Path

from ..config import Config, quiet_third_party_logs
from ..throttle import Stopped, Throttle, apply_process_priority, rss_bytes
from .job import Chunk, Result, Task, run_job

JOIN_SECONDS = 5.0


class InlineRunner:
    """One job at a time, in the calling process. A job advances by one message per poll()."""

    def __init__(self, cfg: Config, throttle: Throttle) -> None:
        self._cfg = cfg
        self._throttle = throttle
        self._job = None
        self.wants_recycle = False

    def has_slot(self) -> bool:
        return self._job is None

    def submit(self, task: Task) -> None:
        assert self._job is None, "the inline runner takes one job at a time"
        self._job = run_job(task, self._cfg, self._throttle)

    def poll(self, timeout: float) -> list[Chunk | Result]:  # timeout: same signature as the pool's
        assert self._job is not None, "poll() without a job"
        msg = next(self._job)
        if isinstance(msg, Result):
            self._job = None
            self.wants_recycle |= msg.recycle
        return [msg]

    def close(self) -> None:
        if self._job is not None:
            self._job.close()  # runs the generator's cleanup (closes the extractor)
            self._job = None


@dataclass(slots=True)
class _Worker:
    process: BaseProcess
    conn: Connection
    job: int | None = None


class ProcessPool:
    """Up to `size` worker processes, started when first needed."""

    wants_recycle = False  # a worker that needs replacing is replaced here, invisibly

    def __init__(self, cfg: Config, size: int, paused_flag: Path, throttle: Throttle) -> None:
        assert size > 0, "a pool needs at least one worker"
        self._size = size
        # read_mb_per_sec is a limit for the whole indexer: each worker gets its share
        self._cfg = dataclasses.replace(cfg, read_mb_per_sec=cfg.read_mb_per_sec / size)
        self._paused_flag = paused_flag
        self._throttle = throttle  # the indexer's: its clock must not count the time spent waiting
        # spawn: no fork on Windows, and a fresh interpreter has no inherited handles or locks
        self._ctx = multiprocessing.get_context("spawn")
        self._stop = self._ctx.Event()
        self._workers: list[_Worker] = []

    def has_slot(self) -> bool:
        return len(self._workers) < self._size or any(w.job is None for w in self._workers)

    def submit(self, task: Task) -> None:
        while True:
            worker = next((w for w in self._workers if w.job is None), None)
            if worker is None:
                assert len(self._workers) < self._size, "submit() without a free slot"
                worker = self._spawn()
            try:
                worker.conn.send(task)
            except OSError:
                self._discard(worker)  # an idle worker that died (killed from outside): try another
                continue
            worker.job = task.job
            return

    def poll(self, timeout: float) -> list[Chunk | Result]:
        busy = [w for w in self._workers if w.job is not None]
        if not busy:
            return []
        # the process handle is watched as well as the pipe: a worker that dies before it has finished
        # starting never closes its end of the pipe (Windows hands the handle over when the child starts)
        by_handle = {h: w for w in busy for h in (w.conn, w.process.sentinel)}
        ready = wait(list(by_handle), timeout)
        self._throttle.resume_clock()
        out: list[Chunk | Result] = []
        seen: set[int] = set()
        for handle in ready:
            worker = by_handle[handle]
            if id(worker) in seen:
                continue
            seen.add(id(worker))
            msg = self._receive(worker)
            if msg is None:
                # the worker is gone in the middle of a file (killed, or crashed hard). Record the
                # file as failed rather than retrying: a file that kills its parser would do it forever
                job = worker.job
                code = self._discard(worker)
                out.append(Result(job, "error", f"extractor process died (exit code {code})"))
                continue
            if isinstance(msg, Result):
                worker.job = None
                if msg.recycle:
                    self._discard(worker)  # it exits by itself after sending this
            out.append(msg)
        return out

    @staticmethod
    def _receive(worker: _Worker) -> Chunk | Result | None:
        """The next message of a worker, or None when it has nothing more to say (it is gone)."""
        try:
            return worker.conn.recv() if worker.conn.poll() else None
        except (EOFError, OSError):
            return None

    def close(self) -> None:
        self._stop.set()
        workers, self._workers = self._workers, []
        for w in workers:
            try:
                w.conn.send(None)
            except OSError:
                pass  # already gone; closing the pipe below stops a worker blocked in send()
            w.conn.close()  # a worker blocked in send() gets an error and exits
        deadline = time.monotonic() + JOIN_SECONDS
        for w in workers:
            self._reap(w, deadline - time.monotonic())

    def _spawn(self) -> _Worker:
        parent, child = self._ctx.Pipe()
        # daemon: if the indexer exits without close(), multiprocessing terminates the workers; if it is
        # killed, they notice the closed pipe and exit
        process = self._ctx.Process(target=_serve, args=(child, self._cfg, self._stop, self._paused_flag),
                                    name="sindex-extract", daemon=True)
        process.start()
        child.close()  # only the worker holds this end, so a dying worker shows up as EOF here
        worker = _Worker(process, parent)
        self._workers.append(worker)
        return worker

    def _discard(self, worker: _Worker) -> int | None:
        """Forget a worker and make sure its process is gone. Returns its exit code."""
        self._workers.remove(worker)
        worker.conn.close()
        return self._reap(worker, JOIN_SECONDS)

    @staticmethod
    def _reap(worker: _Worker, timeout: float) -> int | None:
        worker.process.join(max(0.0, timeout))
        if worker.process.is_alive():
            worker.process.terminate()
            worker.process.join(JOIN_SECONDS)
        return worker.process.exitcode


def _serve(conn: Connection, cfg: Config, stop, paused_flag: Path) -> None:  # stop: a multiprocessing Event
    """Worker process: take tasks from the indexer until told to stop, recycling itself when it has grown."""
    apply_process_priority(cfg)
    quiet_third_party_logs()
    throttle = Throttle(cfg, stop, extra_pause=lambda: "paused by user" if paused_flag.exists() else None)
    done = 0
    try:
        while (task := conn.recv()) is not None:
            recycle = False
            for msg in run_job(task, cfg, throttle):
                if isinstance(msg, Result):
                    done += 1
                    recycle = (msg.recycle or done >= cfg.recycle_after_files
                               or rss_bytes() > cfg.recycle_rss_mb * 1048576)
                    msg = dataclasses.replace(msg, recycle=recycle)  # the flag is decided here, not in run_job
                conn.send(msg)
            if recycle:
                return
    except (Stopped, KeyboardInterrupt, EOFError, OSError):
        return  # asked to stop, or the indexer went away (a closed pipe raises EOFError or OSError)


def make_runner(cfg: Config, throttle: Throttle, paused_flag: Path) -> InlineRunner | ProcessPool:
    if cfg.extract_workers > 0:
        return ProcessPool(cfg, cfg.extract_workers, paused_flag, throttle)
    return InlineRunner(cfg, throttle)
