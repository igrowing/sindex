"""Keeping the indexer cheap: priorities, EcoQoS, Job Object caps, duty cycle, pausing."""

from __future__ import annotations

import _winapi
import ctypes
import logging
import msvcrt
import threading
import time
from collections.abc import Callable
from ctypes import wintypes

from .config import Config

log = logging.getLogger(__name__)

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

IDLE_PRIORITY_CLASS = 0x40
PROCESS_MODE_BACKGROUND_BEGIN = 0x00100000
ProcessPowerThrottling = 4
PROCESS_POWER_THROTTLING_CURRENT_VERSION = 1
PROCESS_POWER_THROTTLING_EXECUTION_SPEED = 0x1
JobObjectExtendedLimitInformation = 9
JobObjectCpuRateControlInformation = 15
JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x100
JOB_OBJECT_CPU_RATE_CONTROL_ENABLE = 0x1
JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP = 0x4


class Stopped(Exception):
    """Raised from a checkpoint when the process has been asked to stop."""


class _PowerThrottlingState(ctypes.Structure):
    _fields_ = [("Version", wintypes.ULONG), ("ControlMask", wintypes.ULONG), ("StateMask", wintypes.ULONG)]


class _IoCounters(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _BasicLimit(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _ExtendedLimit(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimit),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _CpuRateControl(ctypes.Structure):
    _fields_ = [("ControlFlags", wintypes.DWORD), ("CpuRate", wintypes.DWORD)]


class _MemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


class _SystemPowerStatus(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
        ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
        ("BatteryLifeTime", wintypes.DWORD), ("BatteryFullLifeTime", wintypes.DWORD),
    ]


kernel32.GetCurrentProcess.restype = wintypes.HANDLE
kernel32.CreateJobObjectW.restype = wintypes.HANDLE
kernel32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(_MemoryCounters), wintypes.DWORD]
kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
kernel32.SetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
kernel32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]

_job_handle = None  # kept alive for the lifetime of the process


def _check(ok, what: str) -> bool:
    if not ok:
        log.warning("%s failed: WinError %d", what, ctypes.get_last_error())
    return bool(ok)


def apply_process_priority(cfg: Config) -> None:
    """Priority, I/O mode and EcoQoS of the current process (see the [throttle] config).

    Extraction workers call this on their own: background I/O mode and EcoQoS are not inherited
    from the parent, only the priority class is.
    """
    proc = kernel32.GetCurrentProcess()
    # Measured on an idle machine, indexing the same files: EcoQoS about 2x slower, background
    # I/O mode about 1.5x, idle CPU priority no cost until something else wants the CPU.
    if cfg.background_io:
        # lowers I/O and memory priority; it also resets the CPU priority class to NORMAL,
        # so IDLE must be set after it
        _check(kernel32.SetPriorityClass(proc, PROCESS_MODE_BACKGROUND_BEGIN), "SetPriorityClass(BACKGROUND)")
    if cfg.idle_priority:
        _check(kernel32.SetPriorityClass(proc, IDLE_PRIORITY_CLASS), "SetPriorityClass(IDLE)")

    # explicit both ways: turning it off must also opt out of throttling Windows may apply itself
    eco = _PowerThrottlingState(PROCESS_POWER_THROTTLING_CURRENT_VERSION,
                                PROCESS_POWER_THROTTLING_EXECUTION_SPEED,
                                PROCESS_POWER_THROTTLING_EXECUTION_SPEED if cfg.efficiency_mode else 0)
    _check(kernel32.SetProcessInformation(proc, ProcessPowerThrottling, ctypes.byref(eco), ctypes.sizeof(eco)),
           "EcoQoS")


def apply_process_limits(cfg: Config) -> None:
    """Make the current process a polite background citizen, and cap it (and its children) in a Job Object."""
    global _job_handle
    apply_process_priority(cfg)
    proc = kernel32.GetCurrentProcess()
    if cfg.memory_limit_mb <= 0 and cfg.cpu_rate_percent <= 0:
        return
    # extraction workers are started after this, so they join the job: the CPU cap is shared by all of
    # them, the memory cap applies to each process on its own
    job = kernel32.CreateJobObjectW(None, None)
    if not _check(job, "CreateJobObject"):
        return
    if cfg.memory_limit_mb > 0:
        info = _ExtendedLimit()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_PROCESS_MEMORY
        info.ProcessMemoryLimit = cfg.memory_limit_mb * 1024 * 1024
        _check(kernel32.SetInformationJobObject(job, JobObjectExtendedLimitInformation,
                                                ctypes.byref(info), ctypes.sizeof(info)), "job memory limit")
    if cfg.cpu_rate_percent > 0:
        rate = _CpuRateControl(JOB_OBJECT_CPU_RATE_CONTROL_ENABLE | JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP,
                               max(1, min(100, cfg.cpu_rate_percent)) * 100)
        _check(kernel32.SetInformationJobObject(job, JobObjectCpuRateControlInformation,
                                                ctypes.byref(rate), ctypes.sizeof(rate)), "job CPU cap")
    if _check(kernel32.AssignProcessToJobObject(job, proc), "AssignProcessToJobObject"):
        _job_handle = job


def stdin_closed() -> bool:
    """Whether the writing end of our stdin pipe is closed: the supervisor asked us to stop, or is gone.

    This peeks instead of blocking in a read. A thread parked in a synchronous read on the pipe stalls
    the start-up of every process this one spawns: the extraction workers never got as far as running
    Python.
    """
    try:
        _winapi.PeekNamedPipe(msvcrt.get_osfhandle(0), 0)
    except OSError:  # BrokenPipeError: nobody can write to it any more
        return True
    return False


def rss_bytes() -> int:
    counters = _MemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    if kernel32.K32GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        return counters.WorkingSetSize
    return 0


def on_battery() -> bool:
    status = _SystemPowerStatus()
    if not kernel32.GetSystemPowerStatus(ctypes.byref(status)):
        return False
    return status.ACLineStatus == 0


class _CpuMeter:
    """System-wide CPU usage between successive calls."""

    def __init__(self) -> None:
        self._last = self._times()

    @staticmethod
    def _times() -> tuple[int, int, int]:
        idle, kern, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
        kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user))

        def conv(ft: wintypes.FILETIME) -> int:
            return (ft.dwHighDateTime << 32) | ft.dwLowDateTime

        return conv(idle), conv(kern), conv(user)

    def percent(self) -> float:
        now = self._times()
        idle, kern, user = (a - b for a, b in zip(now, self._last, strict=True))
        self._last = now
        total = kern + user  # kernel time includes idle time
        return 0.0 if total <= 0 else 100.0 * (total - idle) / total


class Throttle:
    """Cooperative pacing. Call `checkpoint()` often; it sleeps, pauses and stops."""

    SLICE = 0.1  # seconds of work between duty-cycle sleeps
    PAUSE_CHECK_EVERY = 5.0

    def __init__(self, cfg: Config, stop: threading.Event,
                 extra_pause: Callable[[], str | None] | None = None,
                 on_pause: Callable[[str | None], None] | None = None) -> None:
        self.cfg = cfg
        self.stop = stop
        self.extra_pause = extra_pause  # e.g. "paused by user"
        self.on_pause = on_pause  # notified when the pause state changes
        self.paused_reason: str | None = None
        self._work_start = time.monotonic()
        self._next_pause_check = 0.0
        self._cpu = _CpuMeter()
        self._bytes_budget = 0.0
        self._budget_at = time.monotonic()

    def sleep(self, seconds: float) -> None:
        if self.stop.wait(seconds):
            raise Stopped
        self._work_start = time.monotonic()  # sleeping is not work

    def resume_clock(self) -> None:
        """Time spent waiting for other processes is not work: count from now on."""
        self._work_start = time.monotonic()

    def checkpoint(self) -> None:
        if self.stop.is_set():
            raise Stopped
        now = time.monotonic()
        worked = now - self._work_start
        if worked >= self.SLICE:
            if self.cfg.duty_factor > 0:
                self.sleep(min(worked * self.cfg.duty_factor, 5.0))
            if now >= self._next_pause_check:
                self.wait_while_paused()
            self._work_start = time.monotonic()

    def consume(self, nbytes: int) -> None:
        """Account for bytes read from disk; sleeps to respect read_mb_per_sec."""
        rate = self.cfg.read_mb_per_sec * 1024 * 1024
        if rate > 0:
            now = time.monotonic()
            self._bytes_budget = min(rate, self._bytes_budget + (now - self._budget_at) * rate) - nbytes
            self._budget_at = now
            if self._bytes_budget < 0:
                self.sleep(-self._bytes_budget / rate)
        self.checkpoint()

    def pause_reason(self) -> str | None:
        if self.extra_pause and (reason := self.extra_pause()):
            return reason
        if self.cfg.pause_on_battery and on_battery():
            return "on battery"
        limit = self.cfg.pause_when_cpu_busy_percent
        if limit > 0 and self._cpu.percent() > limit:
            return "system busy"
        return None

    def wait_while_paused(self) -> None:
        while (reason := self.pause_reason()) is not None:
            if reason != self.paused_reason:
                log.info("pausing: %s", reason)
                self.paused_reason = reason
                if self.on_pause:
                    self.on_pause(reason)
            self.sleep(5.0)
        if self.paused_reason:
            log.info("resuming")
            self.paused_reason = None
            if self.on_pause:
                self.on_pause(None)
        self._next_pause_check = time.monotonic() + self.PAUSE_CHECK_EVERY


class ThrottledReader:
    """Binary file wrapper that paces reads through a Throttle (seekable)."""

    def __init__(self, raw, throttle: Throttle) -> None:
        self._raw = raw
        self._throttle = throttle

    def read(self, n: int = -1) -> bytes:
        data = self._raw.read(n)
        self._throttle.consume(len(data))
        return data

    def readinto(self, buf) -> int:
        n = self._raw.readinto(buf)
        self._throttle.consume(n or 0)
        return n

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._raw.seek(offset, whence)

    def tell(self) -> int:
        return self._raw.tell()

    def seekable(self) -> bool:
        return True

    def readable(self) -> bool:
        return True

    def close(self) -> None:
        self._raw.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()
