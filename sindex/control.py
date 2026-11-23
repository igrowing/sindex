"""File-based control of the indexer (see indexer.py) and read-only statistics."""

from __future__ import annotations

import heapq
import json
import logging
import os
import sqlite3
import subprocess
import time
import uuid
from dataclasses import dataclass

from . import config, db
from .config import Config
from .usn import fixed_drives

log = logging.getLogger(__name__)

RETRY_STATUSES = ("error", "too_big", "binary", "truncated", "empty")  # what the health list can show
USE_ROWS = 200  # folders disk_use() lists below one folder; the others are only counted
USE_TOP = 30  # folders it lists by default when ranking all folders
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


def request_data_folder(cfg: Config, folder: str, mode: str) -> None:
    """Ask the supervisor to copy the data to `folder` ("copy") or to switch to the data in it ("use"). It acts
    with the indexer stopped, because only the indexer writes the database and a file in use cannot be copied whole."""
    request_settings(cfg, {"data_folder": {"folder": folder, "mode": mode}})


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
    if listed := conn.execute("SELECT count(*) FROM names").fetchone()[0]:
        by_status[db.NAME_ONLY] = listed
    queued = conn.execute("SELECT count(*) FROM queue").fetchone()[0]
    return {"files": by_status, "total_files": sum(by_status.values()), "queued": queued,
            "db_mb": round(db_size_bytes(cfg) / 1048576, 1), "db_path": str(cfg.db_path),
            "data_dir": str(cfg.data_dir), "data_dir_movable": config.data_dir_is_movable(cfg),
            "maintenance_pending": db.get_meta(conn, "maintenance_pending", []),
            "last_maintenance": db.get_meta(conn, "last_maintenance"),
            "media": media_stats(conn)}


@dataclass(slots=True)
class _Folder:
    """A folder's share of the index while disk_use() walks the tree: its own files and, once its subfolders are
    closed, theirs too."""

    path: str
    key: str  # lower case, for comparing
    files: int = 0
    names: int = 0
    text: int = 0
    unmeasured: int = 0
    own_files: int = 0
    own_names: int = 0
    own_text: int = 0
    subfolders: int = 0  # with something indexed in them


def _chain(path: str) -> list[str]:
    """`path` and the folders above it, from the drive down: C:\\, C:\\Users, C:\\Users\\me."""
    drive, rest = os.path.splitdrive(path)
    chain = [drive + "\\"]
    for part in rest.split("\\"):
        if part:
            chain.append(os.path.join(chain[-1], part))
    return chain


def disk_use(conn: sqlite3.Connection, cfg: Config, path: str = "", *, anywhere: bool = False,
             limit: int | None = None) -> dict:
    """What takes the space in the index, by folder, like `du`. A folder counts everything below it.

    Lists the folders right below `path` ("" for the drives), biggest first, or with `anywhere` the biggest folders
    at any depth. The stored text of a folder is exact (files.text_bytes); its share of the whole index file (word
    index, file list and the rest) is estimated as proportional to its text. Files of an older index that the
    indexer has not measured yet count as no text (`unmeasured`).

    The folders come sorted so that every folder is followed by everything below it, and are added up on a stack
    of the folders above the current one: memory is the depth of the tree plus the folders right below `path` (or
    the `limit` biggest), not a total for each of the tens of thousands of folders.
    """
    limit = limit or (USE_TOP if anywhere else USE_ROWS)
    root = os.path.normpath(path) if path else ""
    if root.endswith(":"):  # normpath leaves "C:" without the backslash that makes it the drive's root folder
        root += "\\"
    root_key = root.lower()
    files, text, unmeasured = conn.execute(
        "SELECT count(*), coalesce(sum(text_bytes), 0), coalesce(sum(text_bytes IS NULL), 0) FROM files").fetchone()
    names = conn.execute("SELECT count(*) FROM names").fetchone()[0]
    db_bytes = db_size_bytes(cfg)
    per_text = db_bytes / text if text else 0.0

    where, params = "", []
    if root and not anywhere:
        where, params = "WHERE d.path = ? OR d.path LIKE ? ESCAPE '^'", [root, db.like_under(root)]
    # a backslash sorts after "-" or " ", which would put "C:\x-y" between "C:\x" and "C:\x\a"; as char(1)
    # it sorts before everything, so a folder's subfolders follow it directly
    rows = conn.execute(
        f"""SELECT d.path, coalesce(f.files, 0), coalesce(n.names, 0), coalesce(f.text, 0), coalesce(f.unmeasured, 0)
            FROM dirs d
            LEFT JOIN (SELECT dir_id, count(*) AS files, sum(coalesce(text_bytes, 0)) AS text,
                              sum(text_bytes IS NULL) AS unmeasured FROM files GROUP BY dir_id) f ON f.dir_id = d.id
            LEFT JOIN (SELECT dir_id, count(*) AS names FROM names GROUP BY dir_id) n ON n.dir_id = d.id
            {where}
            ORDER BY replace(d.path, '\\', char(1)) COLLATE NOCASE""", params)

    def row(f: _Folder) -> dict:
        return {"path": f.path, "name": os.path.basename(f.path.rstrip("\\")) or f.path, "files": f.files,
                "names": f.names, "text_bytes": f.text, "own_text_bytes": f.own_text, "unmeasured": f.unmeasured,
                "index_bytes": round(f.text * per_text), "share": f.text / text if text else 0.0}

    def size(f: _Folder) -> tuple[int, int]:
        return f.text, f.files + f.names

    picked: list[tuple[tuple[int, int], int, _Folder]] = []  # anywhere: a heap of the biggest
    below: list[_Folder] = []  # otherwise: the folders right below `root`
    here: _Folder | None = None
    order = 0  # breaks ties in the heap, which cannot compare folders
    stack: list[_Folder] = []

    def close() -> None:
        nonlocal here, order
        f = stack.pop()
        if stack:
            up = stack[-1]
            up.files += f.files
            up.names += f.names
            up.text += f.text
            up.unmeasured += f.unmeasured
        if anywhere:
            if not (f.own_files or f.own_names) and f.subfolders == 1:
                return  # a folder on the way to a single subfolder only repeats that subfolder's numbers
            order += 1
            item = (size(f), order, f)
            if len(picked) < limit:
                heapq.heappush(picked, item)
            elif item[0] > picked[0][0]:
                heapq.heapreplace(picked, item)
        elif f.key == root_key:
            here = f
        elif (stack[-1].key if stack else "") == root_key:
            below.append(f)

    for folder, n_files, n_names, n_text, n_unmeasured in rows:
        chain = _chain(folder)
        keys = [c.lower() for c in chain]
        while stack and (len(stack) > len(keys) or stack[len(stack) - 1].key != keys[len(stack) - 1]):
            close()  # done with the folders that are not on this one's way down
        for depth in range(len(stack), len(chain)):
            if stack:
                stack[-1].subfolders += 1
            stack.append(_Folder(chain[depth], keys[depth]))
        f = stack[-1]
        f.files += n_files
        f.names += n_names
        f.text += n_text
        f.unmeasured += n_unmeasured
        f.own_files, f.own_names, f.own_text = n_files, n_names, n_text
    while stack:
        close()

    if anywhere:
        listed = [f for _, _, f in sorted(picked, key=lambda item: item[0], reverse=True)]
    else:
        listed = sorted(below, key=size, reverse=True)
    chain = _chain(root) if root else []
    return {
        "path": root, "chain": chain, "anywhere": anywhere,
        "db_bytes": db_bytes, "text_bytes": text, "files": files, "names": names, "unmeasured": unmeasured,
        "here": None if here is None or not (here.own_files or here.own_names) else {
            "files": here.own_files, "names": here.own_names, "text_bytes": here.own_text,
            "index_bytes": round(here.own_text * per_text), "share": here.own_text / text if text else 0.0},
        "rows": [row(f) for f in listed[:limit]], "more": max(0, len(listed) - limit),
    }


def problem_files(conn: sqlite3.Connection, status: str, *, limit: int = 100, offset: int = 0) -> list[dict]:
    """Files that ended with `status` (see RETRY_STATUSES), for the health list."""
    if status not in RETRY_STATUSES:
        raise ValueError(f"no such status {status!r}")
    rows = conn.execute("SELECT d.path, f.name, f.size, f.error FROM files f JOIN dirs d ON d.id = f.dir_id "
                        "WHERE f.status = ? ORDER BY d.path, f.name LIMIT ? OFFSET ?", (status, limit, offset))
    return [{"path": os.path.join(d, n), "dir": d, "name": n, "size": size, "error": error}
            for d, n, size, error in rows]
