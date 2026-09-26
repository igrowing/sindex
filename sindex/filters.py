"""Which paths are indexed: roots, exclusions, hidden folders, extensions."""

from __future__ import annotations

import fnmatch
import os
import re
import stat

from .config import Config
from .extract import file_ext, kind_of

A_HIDDEN = stat.FILE_ATTRIBUTE_HIDDEN
A_SYSTEM = stat.FILE_ATTRIBUTE_SYSTEM
A_DIRECTORY = stat.FILE_ATTRIBUTE_DIRECTORY
A_REPARSE = stat.FILE_ATTRIBUTE_REPARSE_POINT
A_OFFLINE = stat.FILE_ATTRIBUTE_OFFLINE
A_RECALL_ON_OPEN = 0x40000
A_RECALL_ON_DATA_ACCESS = 0x400000
# never open these: system files, links, and cloud placeholders (would trigger a download)
SKIP_FILE_ATTRS = A_SYSTEM | A_REPARSE | A_OFFLINE | A_RECALL_ON_OPEN | A_RECALL_ON_DATA_ACCESS


def norm(path: str) -> str:
    path = os.path.normpath(os.path.abspath(path))
    if len(path) == 2 and path[1] == ":":
        path += "\\"
    return path


def _with_sep(path: str) -> str:
    return path if path.endswith("\\") else path + "\\"


def dedupe_roots(roots: list[str]) -> list[str]:
    roots = sorted({norm(r) for r in roots}, key=lambda r: (len(r), r.lower()))
    kept: list[str] = []
    for r in roots:
        if not any(r.lower().startswith(_with_sep(k).lower()) for k in kept):
            kept.append(r)
    return kept


def compile_globs(patterns: list[str]) -> tuple[list[re.Pattern], list[re.Pattern]]:
    r"""Split exclude patterns into (matched against the name, matched against the whole path).

    A pattern with a backslash is a path pattern, one without is about the name alone, so that "*.env"
    and "secret*" work in every folder. `*` also matches across backslashes: "*\.ssh\*" is everything
    below any .ssh folder.
    """
    by_name: list[re.Pattern] = []
    by_path: list[re.Pattern] = []
    for pattern in patterns:
        pattern = pattern.replace("/", "\\")
        (by_path if "\\" in pattern else by_name).append(re.compile(fnmatch.translate(pattern), re.IGNORECASE))
    return by_name, by_path


class PathFilter:
    MAX_CACHE = 20_000

    def __init__(self, cfg: Config, roots: list[str]) -> None:
        self.cfg = cfg
        self.roots = dedupe_roots(roots)
        self._root_prefixes = [_with_sep(r).lower() for r in self.roots]
        self._excluded = [_with_sep(norm(p)).lower() for p in cfg.builtin_exclude_paths() + cfg.exclude_paths]
        self._names = {n.lower() for n in cfg.exclude_dir_names}
        self._name_globs, self._path_globs = compile_globs(cfg.exclude_globs)
        self._hidden_cache: dict[str, bool] = {}

    def root_of(self, path: str) -> str | None:
        low = _with_sep(path).lower()
        for root, prefix in zip(self.roots, self._root_prefixes, strict=True):
            if low.startswith(prefix):
                return root
        return None

    def _excluded_prefix(self, path: str) -> bool:
        low = _with_sep(path).lower()
        return any(low.startswith(p) for p in self._excluded)

    def _glob_excluded(self, path: str, name: str, *, is_dir: bool) -> bool:
        if any(g.match(name) for g in self._name_globs):
            return True
        if not self._path_globs:
            return False
        # a folder also matches with its closing backslash, so that "*\.ssh\*" excludes the .ssh folder itself
        candidates = (path, path + "\\") if is_dir else (path,)
        return any(g.match(c) for g in self._path_globs for c in candidates)

    def skip_dir_entry(self, path: str, name: str, attrs: int) -> bool:
        """For the scanner: a subdirectory found while walking."""
        if attrs & A_REPARSE or name.lower() in self._names:
            return True
        if attrs & A_HIDDEN and not self.cfg.index_hidden:
            return True
        return self._excluded_prefix(path) or self._glob_excluded(path, name, is_dir=True)

    def skip_file_entry(self, path: str, name: str, attrs: int) -> bool:
        """For the scanner: a file found while walking (its extension is already known to be wanted)."""
        if attrs & SKIP_FILE_ATTRS:
            return True
        if attrs & A_HIDDEN and not self.cfg.index_hidden:
            return True
        return self._glob_excluded(path, name, is_dir=False)

    def _hidden_dir(self, path: str) -> bool:
        cached = self._hidden_cache.get(path)
        if cached is None:
            try:
                attrs = os.stat(path, follow_symlinks=False).st_file_attributes
                cached = bool(attrs & (A_HIDDEN | A_REPARSE))
            except OSError:
                cached = False
            if len(self._hidden_cache) >= self.MAX_CACHE:
                self._hidden_cache.clear()
            self._hidden_cache[path] = cached
        return cached

    def dir_excluded(self, path: str) -> bool:
        """Whether a directory (and everything below) is outside the index."""
        root = self.root_of(path)
        if root is None or self._excluded_prefix(path):
            return True
        rel = path[len(root):].strip("\\")
        if not rel:
            return False
        parts = rel.split("\\")
        if any(p.lower() in self._names for p in parts):
            return True
        globs = bool(self._name_globs or self._path_globs)
        if globs or not self.cfg.index_hidden:
            current = root
            for p in parts:
                current = os.path.join(current, p)
                if globs and self._glob_excluded(current, p, is_dir=True):
                    return True
                if not self.cfg.index_hidden and self._hidden_dir(current):
                    return True
        return False

    def file_kind(self, path: str) -> str | None:
        """Extractor kind if `path` belongs in the index, else None."""
        kind = kind_of(file_ext(path), self.cfg)
        if kind is None or self.dir_excluded(os.path.dirname(path)):
            return None
        if self._glob_excluded(path, os.path.basename(path), is_dir=False):
            return None
        return kind
