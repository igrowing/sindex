"""What differs when sindex runs as the PyInstaller executable, and how the executable installs itself."""

import subprocess
import sys

import pytest
import regex

from sindex import cli, service


@pytest.fixture
def frozen(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\Program Files\sindex\sindex.exe")


def test_a_python_environment_starts_the_indexer_as_a_module():
    cmd = service.indexer_command("--data-dir", "X")

    assert cmd[1:] == ["-m", "sindex.indexer", "--data-dir", "X"]


def test_the_executable_starts_the_indexer_as_its_own_command(frozen):
    assert service.indexer_command("--data-dir", "X") == [sys.executable, "indexer", "--data-dir", "X"]


def test_the_supervisor_spawns_the_indexer_the_way_the_build_needs(cfg, frozen, monkeypatch):
    (cfg.data_dir / "logs").mkdir(exist_ok=True)
    seen = {}

    def fake_popen(cmd, **kwargs):
        seen["cmd"], seen["env"] = cmd, kwargs["env"]
        return "process"

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    assert service.Supervisor(cfg, None)._spawn() == "process"

    assert seen["cmd"][:2] == [sys.executable, "indexer"] and "--stdin-stop" in seen["cmd"]
    assert "sindex" not in seen["env"].get("PYTHONPATH", "").lower()  # nothing to import from: it is all bundled


def test_the_indexer_command_of_the_executable_reaches_the_indexer(monkeypatch):
    seen = []
    monkeypatch.setattr("sindex.indexer.main", lambda argv: seen.append(argv) or 0)

    assert cli.main(["indexer", "--data-dir", "X", "--stdin-stop"]) == 0

    assert seen == [["--data-dir", "X", "--stdin-stop"]]


def test_the_internal_commands_are_not_advertised(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])

    commands = regex.search(r"\{([^}]*)\}", capsys.readouterr().out).group(1).split(",")
    assert "install" in commands and not {"service", "indexer"} & set(commands)


def test_opening_the_executable_without_arguments_installs_it(frozen, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["sindex.exe"])
    seen = []
    monkeypatch.setattr(cli, "cmd_install", lambda args: seen.append((args.start, args.wait)) or 0)

    assert cli.main() == 0

    assert seen == [(True, True)]  # start it, and keep the window open to read what happened


def test_a_python_environment_without_arguments_still_asks_for_a_command(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["sindex"])

    with pytest.raises(SystemExit):
        cli.main()


def test_the_program_is_installed_under_program_files(monkeypatch, tmp_path):
    monkeypatch.setenv("ProgramFiles", str(tmp_path))

    assert service.program_dir() == tmp_path / "sindex"


def test_copying_the_program_replaces_the_old_one_completely(tmp_path):
    src, dst = tmp_path / "dist" / "sindex", tmp_path / "Program Files" / "sindex"
    (src / "_internal").mkdir(parents=True)
    (src / "sindex.exe").write_bytes(b"new")
    (src / "_internal" / "lib.dll").write_bytes(b"new lib")
    (dst / "_internal").mkdir(parents=True)
    (dst / "sindex.exe").write_bytes(b"old")
    (dst / "_internal" / "gone-in-the-new-version.dll").write_bytes(b"old lib")

    service.copy_program(src, dst)

    assert (dst / "sindex.exe").read_bytes() == b"new"
    assert (dst / "_internal" / "lib.dll").read_bytes() == b"new lib"
    assert not (dst / "_internal" / "gone-in-the-new-version.dll").exists()


def test_installing_from_the_installed_folder_leaves_it_alone(tmp_path):
    folder = tmp_path / "sindex"
    folder.mkdir()
    (folder / "sindex.exe").write_bytes(b"exe")

    service.copy_program(folder, folder)

    assert (folder / "sindex.exe").read_bytes() == b"exe"


def test_stopping_a_service_that_is_not_installed_is_not_an_error(monkeypatch):
    monkeypatch.setattr(service, "SERVICE_NAME", "sindex-test-service-that-does-not-exist")

    assert service.stop_service() is False
