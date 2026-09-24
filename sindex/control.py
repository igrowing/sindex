"""File-based control of the indexer (see indexer.py) and read-only statistics."""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid

from .config import Config


def pause(cfg: Config) -> None:
    (cfg.data_dir / "paused").touch()


def resume(cfg: Config) -> None:
    (cfg.data_dir / "paused").unlink(missing_ok=True)


def request_reindex(cfg: Config, path: str) -> None:
    requests = cfg.data_dir / "requests"
    requests.mkdir(exist_ok=True)
    tmp = requests / f"{uuid.uuid4().hex}.tmp"
    tmp.write_text(json.dumps({"path": os.path.abspath(path)}), encoding="utf-8")
    os.replace(tmp, tmp.with_suffix(".json"))


def indexer_status(cfg: Config) -> dict:
    try:
        status = json.loads((cfg.data_dir / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        status = {"state": "unknown"}
    status["user_paused"] = (cfg.data_dir / "paused").exists()
    status["age_s"] = round(time.time() - status.get("updated", 0), 1) if "updated" in status else None
    return status


def index_stats(conn: sqlite3.Connection, cfg: Config) -> dict:
    by_status = dict(conn.execute("SELECT status, count(*) FROM files GROUP BY status").fetchall())
    queued = conn.execute("SELECT count(*) FROM queue").fetchone()[0]
    size = sum(p.stat().st_size for p in cfg.data_dir.glob("index.db*") if p.is_file())
    return {"files": by_status, "total_files": sum(by_status.values()), "queued": queued,
            "db_mb": round(size / 1048576, 1)}
