"""File-based control of the indexer (see indexer.py) and read-only statistics."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import time
import uuid

from . import db
from .config import Config
from .usn import fixed_drives

log = logging.getLogger(__name__)

RETRY_STATUSES = ("error", "too_big", "binary", "truncated", "empty")  # what the health list can show
MAINTENANCE_ACTIONS = ("optimize", "vacuum")
SETTINGS_DIR = "settings-requests"  # not requests/: the indexer takes everything in there for a reindex request


def pause(cfg: Config) -> None:
    (cfg.data_dir / "paused").touch()


def resume(cfg: Config) -> None:
    (cfg.data_dir / "paused").unlink(missing_ok=True)


def _request(cfg: Config, payload: dict) -> None:
    requests = cfg.data_dir / "requests"
    requests.mkdir(exist_ok=True)
    tmp = requests / f"{time.time_ns():020d}-{uuid.uuid4().hex}.tmp"  # taken in the order they were made
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, tmp.with_suffix(".json"))


def request_reindex(cfg: Config, path: str) -> None:
    _request(cfg, {"path": os.path.abspath(path)})


def request_retry(cfg: Config, status: str) -> None:
    """Ask the indexer to read every file that ended with this status once more."""
    if status not in RETRY_STATUSES:
        raise ValueError(f"cannot retry files with status {status!r}")
    _request(cfg, {"status": status})


def request_rebuild(cfg: Config) -> None:
    """Ask the indexer to read every file under the roots again."""
    _request(cfg, {"rebuild": True})


def request_maintenance(cfg: Config, action: str) -> None:
    if action not in MAINTENANCE_ACTIONS:
        raise ValueError(f"unknown maintenance action {action!r}")
    _request(cfg, {"maintenance": action})


def request_settings(cfg: Config, changes: dict) -> None:
    """Ask the supervisor to write these (already validated) settings and restart the indexer."""
    requests = cfg.data_dir / SETTINGS_DIR
    requests.mkdir(exist_ok=True)
    name = f"{time.time_ns():020d}-{uuid.uuid4().hex}"  # requests are applied in the order they were made
    tmp = requests / f"{name}.tmp"
    tmp.write_text(json.dumps(changes), encoding="utf-8")
    os.replace(tmp, tmp.with_suffix(".json"))


def take_settings_requests(cfg: Config) -> list[dict]:
    """The pending settings requests, oldest first; they are consumed even when unreadable (and logged)."""
    requests = cfg.data_dir / SETTINGS_DIR
    if not requests.is_dir():
        return []
    taken = []
    for path in sorted(requests.glob("*.json")):
        try:
            taken.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as e:
            log.warning("bad settings request %s: %s", path.name, e)
        path.unlink(missing_ok=True)
    return taken


LOG_FILES = {"service": "service.log", "indexer": "indexer.log", "indexer-stderr": "indexer-stderr.log"}
MAX_LOG_KB = 1024


def read_log(cfg: Config, name: str, kb: int = 256) -> dict:
    """The last `kb` KB of one of the log files, for the log page; the file is only read, never changed.

    Raises ValueError for a name that is not one of LOG_FILES. A log that does not exist yet is empty.
    """
    if name not in LOG_FILES:
        raise ValueError(f"no such log {name!r}")
    limit = max(1, min(kb, MAX_LOG_KB)) * 1024
    path = cfg.data_dir / "logs" / LOG_FILES[name]
    try:
        with path.open("rb") as f:
            size = f.seek(0, os.SEEK_END)
            f.seek(max(0, size - limit))
            data = f.read(limit)
    except FileNotFoundError:
        return {"name": name, "size": 0, "text": "", "truncated": False}
    truncated = size > limit
    if truncated:
        data = data.partition(b"\n")[2]  # the first line is most likely cut in half
    return {"name": name, "size": size, "text": data.decode("utf-8", errors="replace"), "truncated": truncated}


MAX_FOLDERS = 2000  # subfolders returned for one folder; more than this is not something to pick from a list


def list_folders(path: str) -> dict:
    """The subfolders of `path`, for the folder picker of the Settings page; "" lists the fixed drives.

    Raises ValueError (with a message for the user) when `path` is not a folder that can be read.
    """
    if not path:
        return {"path": "", "parent": None, "folders": [{"name": d, "path": d} for d in fixed_drives()],
                "truncated": False}
    if not os.path.isabs(path):
        raise ValueError(f"{path} is not a full path")
    folder = os.path.normpath(path)
    names: list[str] = []
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        names.append(entry.name)
                except OSError as e:
                    log.debug("cannot tell what %s is: %s", entry.path, e)  # skipped like a file would be
    except OSError as e:
        raise ValueError(f"cannot open {folder}: {e.strerror}") from e
    names.sort(key=str.lower)
    parent = os.path.dirname(folder)
    return {"path": folder, "parent": "" if parent == folder else parent,  # above a drive comes the drive list
            "folders": [{"name": n, "path": os.path.join(folder, n)} for n in names[:MAX_FOLDERS]],
            "truncated": len(names) > MAX_FOLDERS}


def open_file(path: str) -> None:
    """Launch `path` in its OS-associated viewer, as a double-click in Explorer would.

    Raises OSError when there is nothing associated with the file type, or when the caller has no desktop to
    show it on: the installed service runs as LocalSystem in Session 0, which Windows isolates from the
    interactive desktop, so this only actually shows something when sindex runs in your own session (for
    example `sindex run` in a console).
    """
    os.startfile(path)  # noqa: S606 -- token-gated like every other mutating action; see web/server.py


def open_folder(path: str) -> None:
    """Show the folder of the file `path` in Explorer, with the file selected when it still exists.

    Like open_file() it needs your own desktop session. FileNotFoundError says the folder is gone, ValueError
    that `path` is not a full path.
    """
    if not os.path.isabs(path):
        raise ValueError(f"{path!r} is not a full path")
    folder = os.path.dirname(path)
    if os.path.exists(path):
        try:
            # A command line string on purpose: Explorer wants quotes around the path only, /select,"<path>".
            # A list would have Python quote "/select,..." as a whole, and Explorer then ignores it and opens
            # Documents. A path cannot hold a quote character. Explorer answers 1 even when it worked.
            subprocess.run(f'explorer.exe /select,"{path}"', check=False, timeout=15)
        except subprocess.TimeoutExpired as e:
            raise OSError("Explorer did not answer") from e
    elif os.path.isdir(folder):
        # token-gated like open_file(); the file is gone, so there is nothing to select
        os.startfile(folder)
    else:
        raise FileNotFoundError(f"the folder {folder} no longer exists")


def indexer_status(cfg: Config) -> dict:
    try:
        status = json.loads((cfg.data_dir / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        status = {"state": "unknown"}
    status["user_paused"] = (cfg.data_dir / "paused").exists()
    status["age_s"] = round(time.time() - status.get("updated", 0), 1) if "updated" in status else None
    return status


def db_size_bytes(cfg: Config) -> int:
    """Size of the index on disk: the database and its write-ahead log."""
    return sum(p.stat().st_size for p in cfg.db_path.parent.glob("index.db*") if p.is_file())


def media_stats(conn: sqlite3.Connection) -> dict:
    return dict(conn.execute("SELECT kind, count(*) FROM media GROUP BY kind").fetchall())


def index_stats(conn: sqlite3.Connection, cfg: Config) -> dict:
    by_status = dict(conn.execute("SELECT status, count(*) FROM files GROUP BY status").fetchall())
    queued = conn.execute("SELECT count(*) FROM queue").fetchone()[0]
    return {"files": by_status, "total_files": sum(by_status.values()), "queued": queued,
            "db_mb": round(db_size_bytes(cfg) / 1048576, 1), "db_path": str(cfg.db_path),
            "maintenance_pending": db.get_meta(conn, "maintenance_pending", []),
            "last_maintenance": db.get_meta(conn, "last_maintenance"),
            "media": media_stats(conn)}


def problem_files(conn: sqlite3.Connection, status: str, *, limit: int = 100, offset: int = 0) -> list[dict]:
    """Files that ended with `status` (see RETRY_STATUSES), for the health list."""
    if status not in RETRY_STATUSES:
        raise ValueError(f"no such status {status!r}")
    rows = conn.execute("SELECT dir, name, size, error FROM files WHERE status = ? ORDER BY dir, name LIMIT ? OFFSET ?",
                        (status, limit, offset))
    return [{"path": os.path.join(d, n), "dir": d, "name": n, "size": size, "error": error}
            for d, n, size, error in rows]
