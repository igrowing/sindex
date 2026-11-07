"""Changing settings from the web UI: request files, the supervisor that applies them, and the HTTP API."""

import json
import subprocess
import sys
import threading
import time

import pytest

from sindex import config, control
from sindex.service import Supervisor

from .conftest import call


def test_settings_requests_are_taken_oldest_first_and_only_once(cfg):
    control.request_settings(cfg, {"duty_factor": 0.5})
    control.request_settings(cfg, {"duty_factor": 0.25})

    assert control.take_settings_requests(cfg) == [{"duty_factor": 0.5}, {"duty_factor": 0.25}]
    assert control.take_settings_requests(cfg) == []


def test_an_unreadable_settings_request_is_dropped(cfg):
    control.request_settings(cfg, {"duty_factor": 0.5})
    (cfg.data_dir / control.SETTINGS_DIR / "1-broken.json").write_text("{not json", encoding="utf-8")

    assert control.take_settings_requests(cfg) == [{"duty_factor": 0.5}]
    assert not list((cfg.data_dir / control.SETTINGS_DIR).iterdir())


def test_the_indexer_does_not_mistake_a_settings_request_for_a_reindex_request(cfg):
    control.request_settings(cfg, {"duty_factor": 0.5})

    assert not (cfg.data_dir / "requests").exists()


def test_supervisor_applies_settings_requests(cfg):
    sup = Supervisor(cfg, threading.Event())
    control.request_settings(cfg, {"duty_factor": 0.5, "extract_workers": 2})

    assert sup._apply_settings() is True

    loaded = config.load(cfg.data_dir)
    assert (loaded.duty_factor, loaded.extract_workers) == (0.5, 2)
    assert sup.settings_error is None
    assert sup._apply_settings() is False  # nothing pending


def test_supervisor_leaves_the_file_alone_when_a_request_is_bad(cfg):
    sup = Supervisor(cfg, threading.Event())
    before = (cfg.data_dir / "config.toml").read_bytes()
    control.request_settings(cfg, {"extract_workers": 99})  # what the web UI would have refused

    assert sup._apply_settings() is False

    assert (cfg.data_dir / "config.toml").read_bytes() == before
    assert "extract_workers" in sup.settings_error


def test_supervisor_reports_a_config_file_that_cannot_be_read(cfg):
    sup = Supervisor(cfg, threading.Event())
    (cfg.data_dir / "config.toml").write_text("this is [not toml", encoding="utf-8")
    control.request_settings(cfg, {"duty_factor": 0.5})

    assert sup._apply_settings() is False

    assert "config.toml" in sup.settings_error


class _FakeIndexerSupervisor(Supervisor):
    """Runs a process that, like the indexer, exits once its stdin is closed."""

    spawned = 0

    def _spawn(self) -> subprocess.Popen:
        type(self).spawned += 1
        return subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE)


def _wait_for(condition, seconds: float = 20.0) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


def test_a_settings_change_restarts_the_indexer_without_the_failure_backoff(cfg):
    stop = threading.Event()
    sup = _FakeIndexerSupervisor(cfg, stop)
    thread = threading.Thread(target=sup.run, daemon=True)  # ends with `stop`, which the test sets
    thread.start()
    try:
        _wait_for(lambda: sup.running)
        first = sup.proc.pid
        control.request_settings(cfg, {"duty_factor": 0.5})

        _wait_for(lambda: sup.restarts == 1 and sup.running)

        assert sup.proc.pid != first
        assert config.load(cfg.data_dir).duty_factor == 0.5
    finally:
        stop.set()
        thread.join(30)
    assert not thread.is_alive()


def test_settings_api_shows_values_specs_and_presets_but_not_the_token(web):
    status, body = call(web, "/api/settings")

    data = json.loads(body)
    assert status == 200
    assert data["values"]["duty_factor"] == 0.1  # what the file says, not the fixture's override
    assert "token" not in data["values"]
    assert data["specs"]["extract_workers"] == {"kind": "int", "lo": 0, "hi": 16, "zero_ok": False, "items": "text"}
    assert set(data["presets"]) == {"background", "balanced", "fast"}
    assert data["preset"] == "custom"
    assert web.token not in body.decode("utf-8")


def test_settings_change_needs_the_token(web):
    status, _ = call(web, "/api/settings", {"changes": {"duty_factor": 0.5}})
    assert status == 403
    status, _ = call(web, "/api/settings", {"changes": {"duty_factor": 0.5}}, token="wrong")
    assert status == 403
    assert control.take_settings_requests(web) == []


def test_settings_change_is_queued_for_the_supervisor_not_written_by_the_web_layer(web):
    before = (web.data_dir / "config.toml").read_bytes()

    status, body = call(web, "/api/settings", {"changes": {"duty_factor": 0.5, "extract_workers": 2.0}}, "secret")

    assert (status, json.loads(body)) == (200, {"ok": True})
    assert (web.data_dir / "config.toml").read_bytes() == before
    assert control.take_settings_requests(web) == [{"duty_factor": 0.5, "extract_workers": 2}]


@pytest.mark.parametrize("payload", [{"changes": {"extract_workers": 99, "nope": 1}}, {"changes": {}}, {}])
def test_a_bad_settings_change_is_refused_and_nothing_is_queued(web, payload):
    status, body = call(web, "/api/settings", payload, "secret")

    assert status == 400
    assert json.loads(body)["errors"]
    assert control.take_settings_requests(web) == []


def test_bad_values_are_all_named_in_the_answer(web):
    _, body = call(web, "/api/settings", {"changes": {"extract_workers": 99, "duty_factor": -1}}, "secret")

    assert json.loads(body)["errors"].keys() == {"extract_workers", "duty_factor"}


def test_pages_and_stylesheet_are_served(web):
    status, page = call(web, "/settings")
    assert status == 200 and b'const TOKEN = "secret"' in page
    status, css = call(web, "/style.css")
    assert status == 200 and b"--accent" in css
    status, page = call(web, "/")
    assert status == 200 and b'href="/settings"' in page
    assert f"v{config.VERSION}".encode() in page and b"{{" not in page  # the version shows next to the name
    assert f"v{config.VERSION}".encode() in call(web, "/settings")[1]


def test_a_body_that_is_not_an_object_is_a_400_not_a_crash(web):
    for path in ("/api/settings", "/api/pause", "/api/reindex"):
        assert call(web, path, [], "secret")[0] == 400
    assert control.take_settings_requests(web) == []


def test_the_settings_script_quotes_its_attribute_selectors():
    # [data-tab=${name}] is a syntax error for an empty name (a plain /settings has no #tab), which stopped
    # the script before it built any field; only a browser can run the page, so this guards the pattern
    from pathlib import Path

    import regex

    page = (Path(__file__).parent.parent / "sindex" / "web" / "static" / "settings.html").read_text(encoding="utf-8")

    assert not regex.findall(r"\[data-\w+=\$\{", page)
