"""The Windows service: supervises the indexer process and serves the web UI.

This process stays small: it never parses files. The indexer child does all the
work, is restarted whenever it exits to recycle memory, and is stopped by
closing its stdin.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import config, control
from .indexer import EXIT_RECYCLE
from .web.server import WebServer

log = logging.getLogger(__name__)

SERVICE_NAME = "sindex"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CREATE_NO_WINDOW = 0x08000000


def python_exe() -> str:
    """python.exe of this environment, even when hosted by pythonservice.exe."""
    exe = Path(sys.executable)
    if exe.name.lower() in ("python.exe", "pythonw.exe"):
        return str(exe)
    for candidate in (Path(sys.prefix) / "Scripts" / "python.exe", Path(sys.base_prefix) / "python.exe"):
        if candidate.exists():
            return str(candidate)
    raise RuntimeError("cannot find python.exe")


class Supervisor:
    STOP_TIMEOUT = 30

    def __init__(self, cfg: config.Config, stop: threading.Event, extra_args: list[str] | None = None) -> None:
        self.cfg = cfg
        self.stop = stop
        self.extra_args = extra_args or []
        self.proc: subprocess.Popen | None = None
        self.restarts = 0
        self.last_exit: int | None = None
        self.settings_error: str | None = None  # why the last settings request was not applied

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _spawn(self) -> subprocess.Popen:
        cmd = [python_exe(), "-m", "sindex.indexer", "--data-dir", str(self.cfg.data_dir),
               "--stdin-stop", *self.extra_args]
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(PROJECT_ROOT), env.get("PYTHONPATH")]))
        stderr = open(self.cfg.data_dir / "logs" / "indexer-stderr.log", "ab")
        try:
            return subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=stderr,
                                    env=env, cwd=str(self.cfg.data_dir), creationflags=CREATE_NO_WINDOW)
        finally:
            stderr.close()

    def _terminate(self, proc: subprocess.Popen) -> None:
        try:
            proc.stdin.close()  # asks the indexer to finish the current file and exit
        except OSError as e:
            log.debug("indexer stdin already closed: %s", e)  # it has exited on its own
        try:
            proc.wait(self.STOP_TIMEOUT)
        except subprocess.TimeoutExpired:
            log.warning("indexer did not stop in time; killing it")
            # /T: python.exe of a venv is a launcher with the real interpreter as its child
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, creationflags=CREATE_NO_WINDOW)
            proc.wait()

    def _apply_settings(self) -> bool:
        """Write the settings the web UI asked for; True if config.toml changed, so the indexer must restart.

        The web UI validated them already. A request can still fail here, for example when config.toml was
        edited by hand in the meantime; that is logged and shown in the status, and the file is left as it was.
        """
        changed = False
        for changes in control.take_settings_requests(self.cfg):
            try:
                config.apply(self.cfg.data_dir, changes)
            except (config.ConfigError, OSError) as e:
                log.error("settings not applied: %s", e)
                self.settings_error = str(e)
            else:
                log.info("settings applied: %s", ", ".join(changes))
                self.settings_error = None
                changed = True
        return changed

    def _wait_before_restart(self, seconds: float) -> bool:
        """Sleep before restarting a failed indexer, but not through a settings change (it may be the fix).

        Returns True when the service is stopping.
        """
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.stop.wait(1.0):
                return True
            if self._apply_settings():
                break
        return False

    def run(self) -> None:
        backoff = 30.0
        while not self.stop.is_set():
            started = time.monotonic()
            self.proc = self._spawn()
            log.info("indexer started (pid %d)", self.proc.pid)
            settings_changed = False
            while self.proc.poll() is None:
                if self.stop.wait(1.0):
                    self._terminate(self.proc)
                    log.info("indexer stopped")
                    return
                if self._apply_settings():
                    log.info("restarting the indexer to apply the new settings")
                    self._terminate(self.proc)
                    settings_changed = True
            self.last_exit = code = self.proc.returncode
            if code == EXIT_RECYCLE or settings_changed:
                self.restarts += 1
                continue
            if time.monotonic() - started > 600:
                backoff = 30.0
            log.log(logging.INFO if code == 0 else logging.ERROR,
                    "indexer exited with code %s; restarting in %.0f s", code, backoff)
            if self._wait_before_restart(backoff):
                return
            backoff = min(backoff * 2, 900.0)


def run_core(cfg: config.Config, stop: threading.Event, extra_args: list[str] | None = None) -> None:
    supervisor = Supervisor(cfg, stop, extra_args)
    web = WebServer(cfg, supervisor)
    web.start()
    try:
        supervisor.run()
    finally:
        web.stop()


try:
    import servicemanager
    import win32service
    import win32serviceutil
except ImportError:  # pywin32 missing: console mode still works
    win32serviceutil = None

if win32serviceutil is not None:
    class SindexService(win32serviceutil.ServiceFramework):
        _svc_name_ = SERVICE_NAME
        _svc_display_name_ = "sindex content indexer"
        _svc_description_ = "Indexes the text content of local files with minimal CPU and memory use."

        def __init__(self, args) -> None:
            super().__init__(args)
            self.stop_event = threading.Event()

        def SvcStop(self) -> None:
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING, waitHint=Supervisor.STOP_TIMEOUT * 1000)
            self.stop_event.set()

        def SvcDoRun(self) -> None:
            servicemanager.LogMsg(servicemanager.EVENTLOG_INFORMATION_TYPE, servicemanager.PYS_SERVICE_STARTED,
                                  (self._svc_name_, ""))
            try:
                cfg = config.load()
                config.setup_logging(cfg.data_dir, "service")
                run_core(cfg, self.stop_event)
            except Exception:
                logging.getLogger(__name__).exception("service failed")
                raise
