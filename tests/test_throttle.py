import json
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest

from sindex.throttle import Throttle, in_schedule

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Runs in a child process: apply_process_limits changes the priority of the whole process.
CHILD = """
import ctypes, json, sys
from ctypes import wintypes
from sindex import config, throttle

cfg = config.Config()
for key, value in json.loads(sys.argv[1]).items():
    setattr(cfg, key, value)
cfg.memory_limit_mb = 0
cfg.cpu_rate_percent = 0  # no Job Object here
throttle.apply_process_limits(cfg)

k = throttle.kernel32
proc = k.GetCurrentProcess()
k.GetPriorityClass.restype = wintypes.DWORD
k.GetPriorityClass.argtypes = [wintypes.HANDLE]
k.GetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
state = throttle._PowerThrottlingState(1, 0, 0)
ok = k.GetProcessInformation(proc, throttle.ProcessPowerThrottling, ctypes.byref(state), ctypes.sizeof(state))
print(json.dumps({"priority": k.GetPriorityClass(proc), "eco": bool(state.StateMask & 1), "eco_readable": bool(ok)}))
"""
NORMAL_PRIORITY_CLASS = 0x20
IDLE_PRIORITY_CLASS = 0x40


def process_state(**settings) -> dict:
    out = subprocess.run([sys.executable, "-c", CHILD, json.dumps(settings)], cwd=PROJECT_ROOT,
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_process_limits_all_on():
    state = process_state(efficiency_mode=True, background_io=True, idle_priority=True)
    assert state["priority"] == IDLE_PRIORITY_CLASS
    if state["eco_readable"]:
        assert state["eco"]


def test_process_limits_can_all_be_turned_off():
    state = process_state(efficiency_mode=False, background_io=False, idle_priority=False)
    assert state["priority"] == NORMAL_PRIORITY_CLASS
    if state["eco_readable"]:
        assert not state["eco"], "turning it off must opt out of EcoQoS explicitly"


def test_switches_are_independent():
    state = process_state(efficiency_mode=False, background_io=False, idle_priority=True)
    assert state["priority"] == IDLE_PRIORITY_CLASS
    if state["eco_readable"]:
        assert not state["eco"]


@pytest.fixture
def slept(cfg, monkeypatch):
    """A Throttle that records how long it would have slept instead of sleeping."""
    throttle = Throttle(cfg, threading.Event())
    naps: list[float] = []
    monkeypatch.setattr(throttle, "sleep", naps.append)
    return throttle, naps


@pytest.mark.parametrize("factor,expected", [(1.0, 0.5), (0.5, 0.25), (2.0, 1.0)])
def test_duty_cycle_sleeps_a_fraction_of_the_work_time(cfg, slept, factor, expected):
    throttle, naps = slept
    cfg.duty_factor = factor
    throttle._work_start = time.monotonic() - 0.5  # 0.5 s of work since the last sleep
    throttle.checkpoint()
    assert naps and naps[0] == pytest.approx(expected, abs=0.05)


def test_duty_cycle_off_never_sleeps(cfg, slept):
    throttle, naps = slept
    cfg.duty_factor = 0
    throttle._work_start = time.monotonic() - 10
    throttle.checkpoint()
    assert naps == []


def test_duty_cycle_sleep_is_capped(cfg, slept):
    throttle, naps = slept
    cfg.duty_factor = 3.0
    throttle._work_start = time.monotonic() - 10
    throttle.checkpoint()
    assert naps[0] == 5.0


def test_read_rate_limit_sleeps_when_over_budget(cfg, slept):
    throttle, naps = slept
    cfg.read_mb_per_sec = 1.0
    throttle.consume(3 * 1024 * 1024)  # the budget starts empty, so 3 MB at 1 MB/s takes 3 s
    assert naps and naps[0] == pytest.approx(3.0, abs=0.2)


def test_read_rate_limit_off(cfg, slept):
    throttle, naps = slept
    cfg.read_mb_per_sec = 0
    throttle.consume(500 * 1024 * 1024)
    assert naps == []


def test_waiting_for_others_is_not_work(cfg, slept):
    throttle, naps = slept
    cfg.duty_factor = 1.0
    throttle._work_start = time.monotonic() - 5.0  # five seconds since the last sleep, all of them spent waiting
    throttle.resume_clock()
    throttle.checkpoint()
    assert naps == []


@pytest.mark.parametrize("windows,at,expected", [
    ([], "12:00", True),  # no schedule: always
    (["09:00-17:00"], "09:00", True),
    (["09:00-17:00"], "16:59", True),
    (["09:00-17:00"], "17:00", False),  # the end is exclusive
    (["09:00-17:00"], "03:00", False),
    (["22:00-06:00"], "23:30", True),  # a window may pass midnight
    (["22:00-06:00"], "00:00", True),
    (["22:00-06:00"], "05:59", True),
    (["22:00-06:00"], "06:00", False),
    (["22:00-06:00"], "12:00", False),
    (["06:00-08:00", "22:00-23:00"], "22:30", True),
    (["06:00-08:00", "22:00-23:00"], "12:00", False),
])
def test_in_schedule(windows, at, expected):
    hour, minute = map(int, at.split(":"))

    assert in_schedule(windows, datetime(2026, 1, 5, hour, minute)) is expected


def _window(minutes_from_now_start: int, minutes_from_now_end: int) -> str:
    now = datetime.now()
    m = now.hour * 60 + now.minute

    def clock(offset: int) -> str:
        return f"{(m + offset) % 1440 // 60:02d}:{(m + offset) % 60:02d}"

    return f"{clock(minutes_from_now_start)}-{clock(minutes_from_now_end)}"


def test_the_indexer_pauses_outside_its_schedule(cfg):
    throttle = Throttle(cfg, threading.Event())
    assert throttle.pause_reason() is None

    cfg.schedule_windows = [_window(60, 120)]  # opens in an hour
    assert throttle.pause_reason() == "outside the schedule"

    cfg.schedule_windows = [_window(-60, 60)]  # open now
    assert throttle.pause_reason() is None
