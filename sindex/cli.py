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
from pathlib import Path

from . import config, control, db
from .search import search

SERVICE_DIR_NAME = "sindex-service"
SHORTCUT_NAME = "sindex search.url"


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

    cfg = _cfg(args)
    cmd = [sys.executable, "-m", "sindex.indexer", "--data-dir", str(cfg.data_dir), "--until-idle", "--console"]
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


def cmd_install(args) -> int:
    if not _is_admin():
        print("run this from an elevated (administrator) prompt", file=sys.stderr)
        return 1
    import winreg

    import win32service
    import win32serviceutil

    from .service import PROJECT_ROOT, SERVICE_NAME, SindexService

    cfg = config.load()  # creates %ProgramData%\sindex\config.toml with a fresh token
    exe = _prepare_service_host()
    win32serviceutil.InstallService(
        "sindex.service.SindexService", SERVICE_NAME, SindexService._svc_display_name_,
        startType=win32service.SERVICE_AUTO_START, exeName=str(exe),
        description=SindexService._svc_description_, delayedstart=True)
    key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, rf"SYSTEM\CurrentControlSet\Services\{SERVICE_NAME}",
                         0, winreg.KEY_SET_VALUE)
    with key:
        winreg.SetValueEx(key, "Environment", 0, winreg.REG_MULTI_SZ, [f"PYTHONPATH={PROJECT_ROOT}"])
    print(f"installed service '{SERVICE_NAME}' (host: {exe}); start it with: sindex start")
    for shortcut in create_shortcuts(f"http://{cfg.web_host}:{cfg.web_port}/", _shortcut_dirs()):
        print(f"created shortcut {shortcut}")
    return 0


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
    for name, func, help_ in (("install", cmd_install, "install the Windows service (admin)"),
                              ("remove", cmd_remove, "remove the Windows service (admin)"),
                              ("start", cmd_start, "start the service"),
                              ("stop", cmd_stop, "stop the service")):
        sub.add_parser(name, help=help_).set_defaults(func=func)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
