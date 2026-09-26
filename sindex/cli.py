"""Command line: run in a console, manage the service, search, control the indexer."""

from __future__ import annotations

import argparse
import ctypes
import html
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from ctypes import wintypes
from pathlib import Path

from . import config, control, db
from .search import search

SERVICE_DIR_NAME = "sindex-service"
SHORTCUT_NAME = "sindex search.url"
ERROR_FAILED_SERVICE_CONTROLLER_CONNECT = 1063


def _cfg(args) -> config.Config:
    return config.load(args.data_dir)


def cmd_run(args) -> int:
    """Service core in the foreground: supervisor + web UI, logs to the console."""
    from .service import run_core

    cfg = _cfg(args)
    config.setup_logging(cfg.data_dir, "service", console=True)
    extra = [a for r in args.root or [] for a in ("--root", r)]
    stop = threading.Event()
    print(f"web UI: http://{cfg.web_host}:{cfg.web_port}/  (Ctrl+C to stop)")
    worker = threading.Thread(target=run_core, args=(cfg, stop, extra), daemon=True)
    worker.start()
    try:
        while worker.is_alive():
            worker.join(0.5)
    except KeyboardInterrupt:
        print("stopping…")
        stop.set()
        worker.join()
    return 0


def cmd_index(args) -> int:
    """Index until there is nothing left to do, in indexer processes that are recycled.

    The indexer asks to be restarted when it has read many files or grown too big. Only a new
    process gives the memory back (a second Indexer in the same process frees nothing), so like
    the service this runs the indexer as a child and starts a fresh one for every recycle.
    """
    from .indexer import EXIT_RECYCLE
    from .service import indexer_command

    cfg = _cfg(args)
    cmd = indexer_command("--data-dir", str(cfg.data_dir), "--until-idle", "--console")
    for root in args.root or []:
        cmd += ["--root", root]
    if args.no_limits:
        cmd.append("--no-limits")
    try:
        while (code := subprocess.call(cmd)) == EXIT_RECYCLE:
            pass
    except KeyboardInterrupt:  # the child received Ctrl+C as well and has stopped
        return 1
    return code


def cmd_search(args) -> int:
    cfg = _cfg(args)
    conn = db.connect(cfg.db_path, readonly=True)
    res = search(conn, " ".join(args.query), path=args.path, exts=args.ext, limit=args.limit,
                 names=args.names)
    if res["error"]:
        print(res["error"], file=sys.stderr)
        return 1
    for r in res["results"]:
        print(r["path"])
        print("    " + html.unescape(re.sub(r"</?mark>", "*", r["snippet"])))
    print(f"-- {res['total']}{'+' if res['capped'] else ''} files")
    return 0


def cmd_status(args) -> int:
    cfg = _cfg(args)
    out = {"indexer": control.indexer_status(cfg)}
    if cfg.db_path.exists():
        conn = db.connect(cfg.db_path, readonly=True)
        out["index"] = control.index_stats(conn, cfg)
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


def cmd_pause(args) -> int:
    control.pause(_cfg(args))
    return 0


def cmd_resume(args) -> int:
    control.resume(_cfg(args))
    return 0


def cmd_reindex(args) -> int:
    cfg = _cfg(args)
    for p in args.paths:
        control.request_reindex(cfg, p)
    return 0


# ---- service management ---------------------------------------------------

def _is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except OSError:
        return False


def _prepare_service_host() -> Path:
    """Copy pythonservice.exe and the DLLs it needs next to each other inside the venv.

    Placed in <venv>/sindex-service/ so Python finds <venv>/pyvenv.cfg one level up
    and runs with this environment's site-packages.
    """
    import win32serviceutil

    site = Path(win32serviceutil.__file__).resolve().parent.parent  # .../site-packages/win32
    prefix = Path(sys.prefix)
    target = prefix / SERVICE_DIR_NAME
    target.mkdir(exist_ok=True)
    base = Path(sys.base_prefix)
    ver = f"{sys.version_info.major}{sys.version_info.minor}"
    sources = [site / "pythonservice.exe",
               site.parent / "pywin32_system32" / f"pywintypes{ver}.dll",
               site.parent / "pywin32_system32" / f"pythoncom{ver}.dll",
               base / f"python{ver}.dll", base / "python3.dll",
               base / "vcruntime140.dll", base / "vcruntime140_1.dll"]
    for src in sources:
        if src.exists():
            shutil.copy2(src, target / src.name)
    return target / "pythonservice.exe"


def _shortcut_dirs() -> list[Path]:
    """All-users Desktop and Start menu: the service is installed for the whole machine, so are its shortcuts."""
    return [Path(os.environ["PUBLIC"]) / "Desktop",
            Path(os.environ["ProgramData"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs"]


def create_shortcuts(url: str, dirs: list[Path]) -> list[Path]:
    made = []
    for folder in dirs:
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / SHORTCUT_NAME
        target.write_text(f"[InternetShortcut]\nURL={url}\n", encoding="utf-8")
        made.append(target)
    return made


def remove_shortcuts(dirs: list[Path]) -> None:
    for folder in dirs:
        (folder / SHORTCUT_NAME).unlink(missing_ok=True)


def _relaunch_elevated(params: list[str]) -> int:
    """Run this executable again with the given arguments after a UAC prompt, in a window of its own."""
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.ShellExecuteW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                      wintypes.LPCWSTR, ctypes.c_int]
    shell32.ShellExecuteW.restype = wintypes.HINSTANCE
    result = int(shell32.ShellExecuteW(None, "runas", sys.executable, subprocess.list2cmdline(params), None, 1) or 0)
    if result <= 32:  # ShellExecute reports failure as a value up to 32
        print(f"could not ask for administrator rights (ShellExecute error {result}, "
              f"WinError {ctypes.get_last_error()})", file=sys.stderr)
        return 1
    return 0


def _install_service(*, start: bool) -> int:
    import pywintypes
    import win32serviceutil

    from . import service

    cfg = config.load()  # creates %ProgramData%\sindex\config.toml with a fresh token
    if service.frozen():
        target = service.program_dir()
        print(f"stopping the service if it runs, then copying the program to {target} ...")
        service.stop_service()
        service.copy_program(Path(sys.executable).parent, target)
        exe = target / Path(sys.executable).name
        existed = service.install_executable_service(exe)
        print(f"{'updated' if existed else 'installed'} service '{service.SERVICE_NAME}' (program: {exe})")
    else:
        import winreg

        import win32service

        exe = _prepare_service_host()
        win32serviceutil.InstallService(
            "sindex.service.SindexService", service.SERVICE_NAME, service.SindexService._svc_display_name_,
            startType=win32service.SERVICE_AUTO_START, exeName=str(exe),
            description=service.SindexService._svc_description_, delayedstart=True)
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                             rf"SYSTEM\CurrentControlSet\Services\{service.SERVICE_NAME}", 0, winreg.KEY_SET_VALUE)
        with key:
            winreg.SetValueEx(key, "Environment", 0, winreg.REG_MULTI_SZ, [f"PYTHONPATH={service.PROJECT_ROOT}"])
        print(f"installed service '{service.SERVICE_NAME}' (host: {exe})")
    url = f"http://{cfg.web_host}:{cfg.web_port}/"
    for shortcut in create_shortcuts(url, _shortcut_dirs()):
        print(f"created shortcut {shortcut}")
    if start:
        try:
            win32serviceutil.StartService(service.SERVICE_NAME)
            print(f"service started; the web UI is at {url}")
        except pywintypes.error as e:
            print(f"could not start the service: {e.strerror}", file=sys.stderr)
            return 1
    else:
        print("start it with: sindex start")
    return 0


def cmd_install(args) -> int:
    from .service import frozen

    if not _is_admin():
        if frozen():  # a self-installing program asks for the rights it needs
            return _relaunch_elevated(["install", *(["--start"] if args.start else []), "--wait"])
        print("run this from an elevated (administrator) prompt", file=sys.stderr)
        return 1
    code = _install_service(start=args.start)
    if args.wait:  # in a window of its own that would close before the messages can be read
        try:
            input("Press Enter to close this window ")
        except EOFError:
            pass  # no console to wait on
    return code


def cmd_service(args) -> int:
    """What the service control manager runs: host the service in this process."""
    import pywintypes
    import servicemanager

    from .service import SindexService

    servicemanager.Initialize()
    servicemanager.PrepareToHostSingle(SindexService)
    try:
        servicemanager.StartServiceCtrlDispatcher()
    except pywintypes.error as e:
        if e.winerror != ERROR_FAILED_SERVICE_CONTROLLER_CONNECT:
            raise
        print("this command is run by Windows when the service starts; to run in a console use: sindex run",
              file=sys.stderr)
        return 1
    return 0


def cmd_indexer(args) -> int:
    """An indexer process, started by the service (the executable's stand-in for `python -m sindex.indexer`)."""
    from .indexer import main as indexer_main

    return indexer_main(args.rest)


def cmd_remove(args) -> int:
    import pywintypes
    import win32serviceutil

    from .service import SERVICE_NAME
    try:
        win32serviceutil.StopService(SERVICE_NAME)
    except pywintypes.error as e:
        print(f"note: could not stop the service first ({e.strerror}); removing it anyway")
    win32serviceutil.RemoveService(SERVICE_NAME)
    remove_shortcuts(_shortcut_dirs())
    print("removed")
    return 0


def cmd_start(args) -> int:
    import win32serviceutil

    from .service import SERVICE_NAME
    win32serviceutil.StartService(SERVICE_NAME)
    return 0


def cmd_stop(args) -> int:
    import win32serviceutil

    from .service import SERVICE_NAME
    win32serviceutil.StopService(SERVICE_NAME)
    return 0


INTERNAL_COMMANDS = {"service": cmd_service, "indexer": cmd_indexer}


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="sindex", description=__doc__)
    ap.add_argument("--data-dir", type=Path, help=r"default: %%ProgramData%%\sindex (or SINDEX_HOME)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="run service core in the console (indexer + web UI)")
    p.add_argument("--root", action="append")
    p.set_defaults(func=cmd_run)
    p = sub.add_parser("index", help="index in this process until done, then exit")
    p.add_argument("--root", action="append")
    p.add_argument("--no-limits", action="store_true", help="skip priority/job limits (faster)")
    p.set_defaults(func=cmd_index)
    p = sub.add_parser("search", help="search from the command line")
    p.add_argument("query", nargs="+")
    p.add_argument("--path")
    p.add_argument("--ext", action="append")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--names", action="store_true", help="also match folder and file names")
    p.set_defaults(func=cmd_search)
    sub.add_parser("status", help="show indexer status").set_defaults(func=cmd_status)
    sub.add_parser("pause", help="pause indexing").set_defaults(func=cmd_pause)
    sub.add_parser("resume", help="resume indexing").set_defaults(func=cmd_resume)
    p = sub.add_parser("reindex", help="re-extract files or folders")
    p.add_argument("paths", nargs="+")
    p.set_defaults(func=cmd_reindex)
    p = sub.add_parser("install", help="install the Windows service (admin)")
    p.add_argument("--start", action="store_true", help="start the service after installing it")
    p.add_argument("--wait", action="store_true", help="wait for Enter at the end (for a window that closes)")
    p.set_defaults(func=cmd_install)
    for name, func, help_ in (("remove", cmd_remove, "remove the Windows service (admin)"),
                              ("start", cmd_start, "start the service"),
                              ("stop", cmd_stop, "stop the service")):
        sub.add_parser(name, help=help_).set_defaults(func=func)

    if argv is None:
        argv = sys.argv[1:]
        if not argv and getattr(sys, "frozen", False):
            argv = ["install", "--start", "--wait"]  # the executable was opened by a double click: install it
    if argv and argv[0] in INTERNAL_COMMANDS:  # run by Windows or by the service, not typed: not in --help
        return INTERNAL_COMMANDS[argv[0]](argparse.Namespace(rest=argv[1:]))
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
