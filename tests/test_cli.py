import sys
import uuid
import winreg
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import regex

from sindex import cli, db


def test_index_command_recycles_in_fresh_processes(tmp_path):
    # `sindex index` used to "recycle" by making a new Indexer inside the same process, which frees
    # no memory: the process kept growing until the job memory cap made every allocation fail
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.toml").write_text(
        "[throttle]\nrecycle_after_files = 3\nduty_factor = 0\nread_mb_per_sec = 0\n"
        "pause_on_battery = false\npause_when_cpu_busy_percent = 0\n", encoding="utf-8")
    root = tmp_path / "docs"
    root.mkdir()
    for i in range(10):
        (root / f"f{i}.txt").write_text(f"document number {i}", encoding="utf-8")

    code = cli.main(["--data-dir", str(data), "index", "--root", str(root), "--no-limits"])

    assert code == 0
    conn = db.connect(data / "index.db", readonly=True)
    assert conn.execute("SELECT count(*) FROM files").fetchone()[0] == 10
    assert conn.execute("SELECT count(*) FROM queue").fetchone()[0] == 0
    log = (data / "logs" / "indexer.log").read_text(encoding="utf-8")
    pids = set(regex.findall(r"indexer\[(\d+)\]", log))
    assert len(pids) >= 3, f"10 files with a recycle every 3 should take at least 3 processes, saw {pids}"


def test_shortcuts_are_created_in_every_folder_and_removed_again(tmp_path):
    desktop, menu = tmp_path / "Desktop", tmp_path / "Start Menu" / "Programs"  # the second does not exist yet

    made = cli.create_shortcuts("http://127.0.0.1:8765/", [desktop, menu])

    assert made == [desktop / cli.SHORTCUT_NAME, menu / cli.SHORTCUT_NAME]
    for shortcut in made:
        text = shortcut.read_text(encoding="utf-8")
        assert text.splitlines() == ["[InternetShortcut]", "URL=http://127.0.0.1:8765/"]

    cli.remove_shortcuts([desktop, menu])
    cli.remove_shortcuts([desktop, menu])  # already gone: not an error
    assert not any(s.exists() for s in made)


def test_shortcut_folders_are_the_all_users_desktop_and_start_menu(monkeypatch, tmp_path):
    monkeypatch.setenv("PUBLIC", str(tmp_path / "Public"))
    monkeypatch.setenv("ProgramData", str(tmp_path / "PD"))

    desktop, menu = cli._shortcut_dirs()

    assert desktop == tmp_path / "Public" / "Desktop"
    assert menu == tmp_path / "PD" / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def test_context_menu_entries_cover_folder_background_and_drive():
    exe = Path(r"C:\Program Files\sindex\sindex.exe")

    entries = cli.context_menu_entries(exe)

    assert entries == {
        "Directory\\shell\\sindex": '"C:\\Program Files\\sindex\\sindex.exe" ui "%1"',
        "Directory\\Background\\shell\\sindex": '"C:\\Program Files\\sindex\\sindex.exe" ui "%V"',
        "Drive\\shell\\sindex": '"C:\\Program Files\\sindex\\sindex.exe" ui "%1"',
    }


def test_register_and_remove_context_menu_round_trip():
    # a private sandbox under HKEY_CURRENT_USER: writing the real Directory/Drive classes here would
    # actually change the developer's own Explorer right-click menu
    base = f"Software\\sindex-test-{uuid.uuid4().hex}"
    exe = Path(r"C:\Program Files\sindex\sindex.exe")

    cli.register_context_menu(exe, root=winreg.HKEY_CURRENT_USER, base=base)
    try:
        for subkey, command in cli.context_menu_entries(exe).items():
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, f"{base}\\{subkey}") as key:
                assert winreg.QueryValueEx(key, None)[0] == cli.CONTEXT_MENU_TEXT
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, f"{base}\\{subkey}\\command") as key:
                assert winreg.QueryValueEx(key, None)[0] == command
    finally:
        cli.remove_context_menu(root=winreg.HKEY_CURRENT_USER, base=base)

    for subkey in cli.context_menu_entries(exe):
        with pytest.raises(FileNotFoundError):
            winreg.OpenKey(winreg.HKEY_CURRENT_USER, f"{base}\\{subkey}")
    cli.remove_context_menu(root=winreg.HKEY_CURRENT_USER, base=base)  # already gone: not an error


def test_register_and_remove_app_path_round_trip():
    key = f"Software\\sindex-test-{uuid.uuid4().hex}\\App Paths\\sindex.exe"
    exe = Path(r"C:\Program Files\sindex\sindex.exe")

    cli.register_app_path(exe, root=winreg.HKEY_CURRENT_USER, key=key)
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
        assert winreg.QueryValueEx(k, None)[0] == str(exe)
        assert winreg.QueryValueEx(k, "Path")[0] == str(exe.parent)

    cli.remove_app_path(root=winreg.HKEY_CURRENT_USER, key=key)
    with pytest.raises(FileNotFoundError):
        winreg.OpenKey(winreg.HKEY_CURRENT_USER, key)
    cli.remove_app_path(root=winreg.HKEY_CURRENT_USER, key=key)  # already gone: not an error


def test_cli_exe_path_is_the_program_files_copy_when_frozen(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("ProgramFiles", r"C:\Program Files")

    assert cli._cli_exe_path() == Path(r"C:\Program Files\sindex\sindex.exe")


def test_cli_exe_path_is_the_venv_console_script_otherwise(monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)

    assert cli._cli_exe_path() == Path(sys.prefix) / "Scripts" / "sindex.exe"


def test_ui_command_opens_the_browser_scoped_to_a_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("SINDEX_HOME", str(tmp_path / "data"))
    opened = []
    monkeypatch.setattr("webbrowser.open", opened.append)

    code = cli.main(["ui", str(tmp_path)])

    assert code == 0
    assert len(opened) == 1
    url = urlsplit(opened[0])
    assert url.path == "/"
    assert parse_qs(url.query)["path"] == [str(tmp_path)]


def test_ui_command_with_a_query(tmp_path, monkeypatch):
    monkeypatch.setenv("SINDEX_HOME", str(tmp_path / "data"))
    opened = []
    monkeypatch.setattr("webbrowser.open", opened.append)

    cli.main(["ui", "--query", "invoice 2024"])

    assert parse_qs(urlsplit(opened[0]).query)["q"] == ["invoice 2024"]


def test_unknown_subcommand_falls_back_to_a_ui_query(tmp_path, monkeypatch):
    # typed in the Explorer address bar or Win+R: `sindex` resolves through App Paths and whatever
    # followed it arrives here as plain argv, with no subcommand of its own (see register_app_path)
    monkeypatch.setenv("SINDEX_HOME", str(tmp_path / "data"))
    opened = []
    monkeypatch.setattr("webbrowser.open", opened.append)

    code = cli.main(["overdue", "invoice"])

    assert code == 0
    assert len(opened) == 1
    assert parse_qs(urlsplit(opened[0]).query)["q"] == ["overdue invoice"]


def test_a_real_subcommand_name_is_not_treated_as_a_query(tmp_path, monkeypatch):
    monkeypatch.setenv("SINDEX_HOME", str(tmp_path / "data"))

    code = cli.main(["status"])  # exercises cmd_status, not the ui fallback

    assert code == 0
