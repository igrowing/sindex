"""Changing the data folder: the folder with config.toml, the index, the logs and the request files.

The folder cannot be named in config.toml, which is inside it; `config.set_data_dir_pointer` records it in the
bootstrap folder, and every start reads it from there. Nothing is deleted from the old folder: the running service
still has its logs open and keeps reading its settings there until it is restarted.
"""

from __future__ import annotations

import os
import shutil
import tomllib
from pathlib import Path

from . import config, db
from .config import Config

MODES = ("copy", "use")  # copy the settings and the index to a new folder, or take the data that is already there
CONFIG_NAME = "config.toml"


class DataFolderError(Exception):
    """The data folder cannot be changed to this, or the change failed; the message says why."""


def _owned_files(cfg: Config) -> list[Path]:
    """What a copy takes along: the settings and, when it is in the data folder, the index (its write-ahead
    log is merged into it first). Logs, status and request files are recreated by the next start."""
    files = [cfg.data_dir / CONFIG_NAME]
    if cfg.db_path.parent == cfg.data_dir and cfg.db_path.exists():
        files.append(cfg.db_path)
    return files


def plan(cfg: Config, folder: str, mode: str) -> Path:
    """Check a change of the data folder and return the folder; DataFolderError says what is wrong.

    The web UI asks before it queues the change and the supervisor asks again when it is about to act. Nothing
    is created or changed here.
    """
    if mode not in MODES:
        raise DataFolderError(f"unknown way to change the data folder: {mode!r}")
    if not config.data_dir_is_movable(cfg):
        raise DataFolderError("the data folder was given with --data-dir or SINDEX_HOME; change that instead")
    if not os.path.isabs(folder):
        raise DataFolderError("the data folder must be a full path such as D:\\sindex-data")
    target = Path(os.path.normpath(folder))
    if os.path.normcase(target) == os.path.normcase(cfg.data_dir):
        raise DataFolderError("that is the data folder already")
    try:
        db.check_location(target / db.INDEX_NAME, "data folder")
    except db.DbLocationError as e:
        raise DataFolderError(str(e)) from e
    settings, index = target / CONFIG_NAME, target / db.INDEX_NAME
    if mode == "copy":
        if settings.exists() or index.exists():
            raise DataFolderError(f"{target} has data of sindex already; use that, or choose another folder")
        return target
    if not settings.exists() and not index.exists():
        raise DataFolderError(f"there is no {CONFIG_NAME} or {db.INDEX_NAME} in {target}")
    if settings.exists():
        try:
            with settings.open("rb") as f:
                tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError) as e:
            raise DataFolderError(f"{settings} cannot be read: {e}") from e
    if index.exists():
        try:
            db.check_index_file(index)
        except db.DbLocationError as e:
            raise DataFolderError(str(e)) from e
    return target


def _copy_to(files: list[Path], target: Path) -> None:
    size = sum(f.stat().st_size for f in files)
    free = shutil.disk_usage(target).free
    if free < size * 1.05:
        raise DataFolderError(f"{target} has {free >> 20} MB free, the data needs {size >> 20} MB")
    done: list[Path] = []
    try:
        for src in files:
            partial = target / (src.name + ".partial")  # the real name appears only when the copy is whole
            shutil.copyfile(src, partial)
            os.replace(partial, target / src.name)
            done.append(target / src.name)
    except OSError as e:
        for copied in done:  # all or nothing: a half copy would make the next try refuse the folder
            copied.unlink(missing_ok=True)
        for partial in target.glob("*.partial"):
            partial.unlink(missing_ok=True)
        raise DataFolderError(f"copying to {target} failed: {e}") from e


def switch(cfg: Config, folder: str, mode: str) -> Path:
    """Do what `plan` checked, and make the folder the data folder of the next start. The indexer must be stopped.
    The old folder is left as it is."""
    target = plan(cfg, folder, mode)
    copied = [target / f.name for f in _owned_files(cfg)] if mode == "copy" else []
    if mode == "copy":
        if cfg.db_path.parent == cfg.data_dir and cfg.db_path.exists():
            try:
                db.checkpoint(cfg.db_path)
            except db.DbLocationError as e:
                raise DataFolderError(str(e)) from e
        _copy_to(_owned_files(cfg), target)
    try:
        config.set_data_dir_pointer(target)
    except OSError as e:
        for f in copied:  # not switched, so the copy would only make the next try refuse the folder
            f.unlink(missing_ok=True)
        raise DataFolderError(f"cannot record the new data folder: {e}") from e
    return target
