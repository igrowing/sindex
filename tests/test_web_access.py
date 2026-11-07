"""The web UI listens on this computer only: web_host must be an IPv4 loopback address."""

import logging
import socket

import pytest

from sindex import cli, config
from sindex.web.server import WebServer


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "127.255.255.254"])
def test_loopback_hosts_are_accepted(host):
    config.check_web_host(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "", "localhost", "::1", "::", "192.168.1.5", "8.8.8.8", "example.com",
                                  "127.1", "127.0.0.1:8765"])
def test_other_hosts_are_refused(host):
    with pytest.raises(config.ConfigError) as e:
        config.check_web_host(host)
    assert "web_host" in e.value.errors


def test_a_config_file_that_opens_the_ui_to_the_network_is_refused(cfg):
    path = cfg.data_dir / "config.toml"
    path.write_text(path.read_text(encoding="utf-8").replace('web_host = "127.0.0.1"', 'web_host = "0.0.0.0"'),
                    encoding="utf-8")
    with pytest.raises(config.ConfigError) as e:
        config.load(cfg.data_dir)
    assert "web_host" in e.value.errors


def test_the_server_does_not_start_on_a_network_address(cfg, caplog):
    cfg.web_host = "0.0.0.0"
    cfg.web_port = _free_port()
    server = WebServer(cfg)
    with caplog.at_level(logging.ERROR):
        server.start()
    assert server._server is None
    assert "web UI unavailable" in caplog.text
    with socket.socket() as s, pytest.raises(OSError):  # and nothing listens
        s.settimeout(1)
        s.connect(("127.0.0.1", cfg.web_port))


def test_a_configuration_error_ends_a_command_with_a_message(cfg, capsys):
    path = cfg.data_dir / "config.toml"
    path.write_text(path.read_text(encoding="utf-8").replace('web_host = "127.0.0.1"', 'web_host = "0.0.0.0"'),
                    encoding="utf-8")
    assert cli.main(["--data-dir", str(cfg.data_dir), "status"]) == 2
    assert "web_host" in capsys.readouterr().err


def test_the_default_server_listens_on_loopback_only(cfg):
    cfg.web_port = _free_port()
    server = WebServer(cfg)
    server.start()
    try:
        assert server._server.server_address[0] == "127.0.0.1"
    finally:
        server.stop()
