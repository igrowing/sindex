"""Configuration: defaults, TOML loading and the data directory."""

from __future__ import annotations

import fnmatch
import functools
import importlib.metadata
import json
import logging
import math
import os
import re
import secrets
import shutil
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field, fields
from logging.handlers import RotatingFileHandler
from pathlib import Path

log = logging.getLogger(__name__)

VERSION = importlib.metadata.version("sindex")

# Markup: tags are stripped and only the text is indexed (extract/text.py). Text that lives in
# attribute values (for example XAML `Content="..."` or `.csproj` package names) is not indexed.
MARKUP_EXTS = frozenset("""
    html htm xhtml shtml xml xsd xsl xslt svg rss atom opml gpx kml xaml fb2 xliff xmp wsdl plist
    csproj vbproj vcxproj fsproj props targets resx nuspec
""".split())
TEXT_EXTS = MARKUP_EXTS | frozenset("""
    txt text md markdown mdx rst adoc org textile csv tsv log ini cfg conf toml yaml yml json jsonc json5
    jsonl ndjson geojson tex bib srt vtt sub ass ssa sbv lrc nfo diz properties sql reg inf ics vcf
    diff patch sln
    py pyw pyi js mjs cjs ts tsx jsx vue svelte css scss sass less
    java kt kts scala groovy gradle c h cc cpp cxx hpp hh hxx cs fs vb vbs go rs rb php
    pl pm lua r swift dart m mm sh bash zsh fish ps1 psm1 psd1 bat cmd asm cmake mk
    proto graphql tf hcl ino pde hs erl ex exs clj cljs ml mli jl nim zig pas f90 vhd vhdl sv tcl awk
    lisp el scm qml coffee styl ejs hbs aspx asp jsp cshtml
""".split())
OFFICE_EXTS = frozenset("""
    docx docm dotx dotm xlsx xlsm xltx xltm pptx pptm potx ppsx
    odt ott ods ots odp otp
""".split())
RTF_EXTS = frozenset({"rtf"})
PDF_EXTS = frozenset({"pdf"})

# Folder names that hold caches, trash and tool state: the start of exclude_globs in a new config
DEFAULT_EXCLUDE_DIR_NAMES = [
    "$recycle.bin", "system volume information", "recovery", "config.msi",
    "node_modules", ".git", ".svn", ".hg", "__pycache__", ".venv", "venv", ".tox",
    ".mypy_cache", ".pytest_cache", ".gradle", ".idea", ".vs", ".cache",
]


@dataclass
class Config:
    # [index]
    roots: list[str] = field(default_factory=list)  # empty = all fixed drives
    exclude_paths: list[str] = field(default_factory=list)  # in addition to built-ins
    exclude_extensions: list[str] = field(default_factory=list)  # never indexed, whatever the folder; may hold * and ?
    # names and patterns of files and folders that are never indexed (see filters.py for what they match)
    exclude_globs: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE_DIR_NAMES))
    index_hidden: bool = False
    text_extensions: list[str] = field(default_factory=lambda: sorted(TEXT_EXTS))  # read as plain text or markup
    max_text_mb: int = 50
    max_office_mb: int = 100
    max_pdf_mb: int = 100
    max_chars_per_file: int = 5_000_000
    chunk_chars: int = 16_000
    # [throttle]
    # process-level politeness of the indexer (each about as costly as it sounds, see throttle.py)
    efficiency_mode: bool = True  # EcoQoS: Windows runs it on low-power settings ("Efficiency mode")
    background_io: bool = True  # low I/O and memory priority
    idle_priority: bool = True  # lowest CPU priority; free on an idle machine
    cpu_rate_percent: int = 10  # hard cap, % of total system CPU; 0 disables
    memory_limit_mb: int = 256  # hard cap for the indexer process; 0 disables
    duty_factor: float = 1.0  # sleep this many times the time spent working
    read_mb_per_sec: float = 4.0  # 0 disables
    pause_on_battery: bool = True
    pause_when_cpu_busy_percent: int = 70  # 0 disables
    schedule_windows: list[str] = field(default_factory=list)  # "HH:MM-HH:MM"; empty = at any time
    recycle_after_files: int = 1000
    recycle_rss_mb: int = 150  # also applies to each extraction worker
    extract_workers: int = 3  # processes that parse files in parallel; 0 = parse in the indexer itself
    # write batching (SSD wear): commit after this many files or seconds, whichever is first
    commit_every_files: int = 200
    commit_every_seconds: int = 60
    wal_autocheckpoint_pages: int = 4000  # 4 KB pages; SQLite's default is 1000
    sqlite_cache_mb: int = 8  # indexer's page cache; also caps how much text one batch may hold
    usn_poll_seconds: int = 300
    rescan_hours: int = 24  # for volumes without a USN journal
    # [web]
    web_host: str = "127.0.0.1"
    web_port: int = 8765
    token: str = ""
    # [storage]
    db_dir: str = ""  # where index.db lives; empty = the data directory

    data_dir: Path = field(default=Path("."), repr=False)

    @property
    def db_path(self) -> Path:
        return (Path(self.db_dir) if self.db_dir else self.data_dir) / "index.db"

    @property
    def text_exts(self) -> frozenset[str]:
        return frozenset(e.lower().lstrip(".") for e in self.text_extensions)

    @property
    def excluded_exts(self) -> frozenset[str]:
        return frozenset(e.lower().lstrip(".") for e in self.exclude_extensions)

    def ext_excluded(self, ext: str) -> bool:
        """Whether files with this extension (lower case, no dot) are excluded; entries may hold * and ?."""
        literals, wild = _ext_matcher(tuple(sorted(self.excluded_exts)))
        return ext in literals or (wild is not None and wild.match(ext) is not None)

    def builtin_exclude_paths(self) -> list[str]:
        paths = [str(self.data_dir), str(self.db_path.parent)]
        for var in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData"):
            if value := os.environ.get(var):
                paths.append(value)
        drive = os.environ.get("SystemDrive", "C:")
        paths += [drive + "\\Windows.old", drive + "\\PerfLogs", drive + "\\$WinREAgent"]
        return paths


# What a new config.toml starts with where it differs from the Config defaults: the defaults are the most
# frugal setting, a fresh file is the faster one that the throttling table in the README recommends.
FRESH_INSTALL = {
    "efficiency_mode": False, "background_io": False, "cpu_rate_percent": 0, "memory_limit_mb": 512,
    "duty_factor": 0.1, "read_mb_per_sec": 0, "pause_on_battery": False, "pause_when_cpu_busy_percent": 80,
    "recycle_after_files": 5000, "recycle_rss_mb": 250, "commit_every_files": 1000, "commit_every_seconds": 120,
    "sqlite_cache_mb": 64,
}

# Throttle presets for the web UI. The numbers are the measured steps in the README: indexing 2,506 files took
# 22 s, 9 s and 3.8 s. Everything else in [throttle] is left as it is.
THROTTLE_PRESETS: dict[str, dict] = {
    "background": {"efficiency_mode": True, "background_io": True, "duty_factor": 1.0, "read_mb_per_sec": 4.0,
                   "cpu_rate_percent": 10},
    "balanced": {"efficiency_mode": False, "background_io": True, "duty_factor": 0.5, "read_mb_per_sec": 4.0,
                 "cpu_rate_percent": 10},
    "fast": {"efficiency_mode": False, "background_io": False, "duty_factor": 0.25, "read_mb_per_sec": 16.0,
             "cpu_rate_percent": 10},
}


def detect_preset(cfg: Config) -> str:
    """Name of the throttle preset that `cfg` matches exactly, or "custom"."""
    for name, values in THROTTLE_PRESETS.items():
        if all(getattr(cfg, key) == value for key, value in values.items()):
            return name
    return "custom"


@dataclass(slots=True, frozen=True)
class Spec:
    """What a setting may be set to from the web UI."""

    kind: type  # bool, int, float or list (of str)
    lo: float | None = None
    hi: float | None = None
    zero_ok: bool = False  # 0 means "off" and is allowed although it is below `lo`
    items: str = "text"  # what each entry of a list is: a key of _ITEM_CHECKS


SPECS: dict[str, Spec] = {
    "roots": Spec(list, items="root"), "exclude_paths": Spec(list, items="path"),
    "exclude_extensions": Spec(list, items="extpat"), "exclude_globs": Spec(list, items="glob"),
    "index_hidden": Spec(bool), "text_extensions": Spec(list, items="ext"),
    "max_text_mb": Spec(int, 1, 100_000), "max_office_mb": Spec(int, 1, 100_000),
    "max_pdf_mb": Spec(int, 1, 100_000), "max_chars_per_file": Spec(int, 1000, 2_000_000_000),
    "chunk_chars": Spec(int, 1000, 1_000_000),
    "efficiency_mode": Spec(bool), "background_io": Spec(bool), "idle_priority": Spec(bool),
    "cpu_rate_percent": Spec(int, 0, 100), "memory_limit_mb": Spec(int, 64, 65_536, zero_ok=True),
    "duty_factor": Spec(float, 0, 10), "read_mb_per_sec": Spec(float, 0, 10_000),
    "pause_on_battery": Spec(bool), "pause_when_cpu_busy_percent": Spec(int, 0, 100),
    "schedule_windows": Spec(list, items="window"),
    "recycle_after_files": Spec(int, 1, 1_000_000), "recycle_rss_mb": Spec(int, 50, 4096),
    "extract_workers": Spec(int, 0, 16),
    "commit_every_files": Spec(int, 1, 100_000), "commit_every_seconds": Spec(int, 1, 3600),
    "sqlite_cache_mb": Spec(int, 1, 1024), "wal_autocheckpoint_pages": Spec(int, 100, 1_000_000),
    "usn_poll_seconds": Spec(int, 5, 86_400), "rescan_hours": Spec(int, 1, 8760),
}


class ConfigError(ValueError):
    """Rejected settings: `errors` maps each bad key to what is wrong with it."""

    def __init__(self, errors: dict[str, str]) -> None:
        super().__init__("; ".join(f"{key}: {why}" for key, why in errors.items()))
        self.errors = errors


_WINDOW = re.compile(r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})")
_EXT = re.compile(r"[\w+~-]+")
_EXT_PATTERN = re.compile(r"[\w+~*?-]+")


@functools.lru_cache(maxsize=8)
def _ext_matcher(entries: tuple[str, ...]) -> tuple[frozenset[str], re.Pattern | None]:
    """Split excluded extensions into plain ones (a set lookup) and patterns (one regular expression)."""
    literals = frozenset(e for e in entries if not re.search(r"[*?]", e))
    wild = [fnmatch.translate(e) for e in entries if re.search(r"[*?]", e)]
    return literals, re.compile("|".join(wild), re.IGNORECASE) if wild else None


def parse_window(text: str) -> tuple[int, int]:
    """(start, end) of a "HH:MM-HH:MM" window as minutes after midnight; it wraps over midnight if end < start."""
    m = _WINDOW.fullmatch(text.strip())
    if m is None:
        raise ValueError("must look like 22:00-06:00")
    h1, m1, h2, m2 = (int(g) for g in m.groups())
    if h1 > 23 or h2 > 23 or m1 > 59 or m2 > 59:
        raise ValueError("hours are 0-23 and minutes 0-59")
    start, end = h1 * 60 + m1, h2 * 60 + m2
    if start == end:
        raise ValueError("starts and ends at the same time")
    return start, end


def _window_item(text: str) -> str:
    start, end = parse_window(text)
    return f"{start // 60:02d}:{start % 60:02d}-{end // 60:02d}:{end % 60:02d}"


def _path_item(text: str) -> str:
    if not os.path.isabs(text):
        raise ValueError("must be a full path such as D:\\Documents")
    return os.path.normpath(text)


def _ext_item(text: str) -> str:
    ext = text.lower().removeprefix("*").lstrip(".")
    if not _EXT.fullmatch(ext):
        raise ValueError(f"{text!r} is not a file extension (write it like pdf or .pdf)")
    return ext


def _ext_pattern_item(text: str) -> str:
    ext = text.lower().removeprefix("*").lstrip(".") if text.startswith(("*.", ".")) else text.lower()
    if not _EXT_PATTERN.fullmatch(ext):
        raise ValueError(f"{text!r} is not a file extension (write it like pdf, .pdf or msi*)")
    if not re.search(r"[^*?]", ext):
        raise ValueError("would exclude every file")
    return ext


def _glob_item(text: str) -> str:
    pattern = text.replace("/", "\\")
    # a dot between wildcards (e.g. "*.*") is not real content either: it still matches almost every
    # file, just like a plain "*" or "?*" would, so treat * ? . \ alone as "no literal name given"
    if re.search(r"[*?]", pattern) and not re.search(r"[^*?.\\]", pattern):
        raise ValueError(f"{text!r} would exclude almost every file; name a folder or a narrower pattern instead")
    return pattern


_ITEM_CHECKS: dict[str, Callable[[str], str]] = {
    "text": lambda text: text, "root": _path_item, "path": _path_item, "ext": _ext_item,
    "extpat": _ext_pattern_item, "glob": _glob_item, "window": _window_item,
}


def _check(key: str, value: object, *, check_paths: bool = False) -> object:
    spec = SPECS.get(key)
    if spec is None:
        raise ConfigError({key: "not a setting that can be changed here"})
    if spec.kind is bool:
        if not isinstance(value, bool):
            raise ConfigError({key: "must be true or false"})
        return value
    if spec.kind is list:
        if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
            raise ConfigError({key: "must be a list of non-empty texts"})
        if spec.items in ("ext", "extpat"):  # extensions may be typed separated by spaces, commas or lines
            value = [word for text in value for word in re.split(r"[\s,;]+", text) if word]
        items: list[str] = []
        for text in value:
            try:
                item = _ITEM_CHECKS[spec.items](text.strip())
            except ValueError as e:
                raise ConfigError({key: str(e)}) from e
            if check_paths and spec.items == "root" and not os.path.isdir(item):
                raise ConfigError({key: f"{item} is not a folder on this computer"})
            if item not in items:
                items.append(item)
        return items
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ConfigError({key: "must be a number"})
    if spec.kind is int:
        if value != int(value):
            raise ConfigError({key: "must be a whole number"})
        value = int(value)
    else:
        value = float(value)
    if not (spec.zero_ok and value == 0):
        if spec.lo is not None and value < spec.lo:
            raise ConfigError({key: f"must be at least {spec.lo:g}" + (" (or 0 = off)" if spec.zero_ok else "")})
        if spec.hi is not None and value > spec.hi:
            raise ConfigError({key: f"must be at most {spec.hi:g}"})
    return value


def validate_changes(changes: object, *, check_paths: bool = False) -> dict[str, object]:
    """The changes with their values in the right type, or a ConfigError naming every bad key.

    `check_paths` also requires every root to exist here; the web UI asks for it, so that a typo is
    refused at once (a root on a drive that is offline later is not an error, it is just skipped).
    """
    if not isinstance(changes, dict) or not changes:
        raise ConfigError({"changes": "must be a non-empty object of setting: value"})
    checked: dict[str, object] = {}
    errors: dict[str, str] = {}
    for key, value in changes.items():
        try:
            checked[key] = _check(str(key), value, check_paths=check_paths)
        except ConfigError as e:
            errors.update(e.errors)
    if errors:
        raise ConfigError(errors)
    return checked


CONFIG_TEMPLATE = """\
# sindex configuration.
# The Settings page of the web UI rewrites this file from a template, so comments you add here are not
# kept (the previous file is saved as config.toml.bak). Changes for the indexer apply when it restarts,
# which the Settings page does for you; after editing by hand, restart the service.

[index]
# Folders to index. Empty = all fixed (non-removable) drives.
roots = {roots}
# Extra folders to skip (Windows, Program Files and ProgramData are always skipped).
exclude_paths = {exclude_paths}
# Files with these extensions (no dot) are never indexed, for example ["env", "pem", "msi*"]; * and ?
# stand for any text and any one character.
exclude_extensions = {exclude_extensions}
# Files and folders with these names are never indexed, wherever they are. An entry is a name
# ("node_modules"), a pattern for names (".*", "secret*", "*.bak"), or, with a backslash, a pattern for
# the whole path ("*\\.ssh\\*", "D:\\Private\\*"). * matches any text, ? one character, case is ignored.
exclude_globs = {exclude_globs}
index_hidden = {index_hidden}
# Files with these extensions are indexed as text (HTML and XML-like ones without their tags). Add your
# own; PDF, Office and RTF files are always read by their own readers.
text_extensions = {text_extensions}
max_text_mb = {max_text_mb}
max_office_mb = {max_office_mb}
max_pdf_mb = {max_pdf_mb}
max_chars_per_file = {max_chars_per_file}
# Text is stored in pieces of about this many characters.
chunk_chars = {chunk_chars}

[throttle]
# How hard the indexer is held back. Each of these slows indexing down; lower/off = faster.
# Process-level switches. On an idle test machine, efficiency_mode alone made indexing about
# 2x slower and background_io about 1.5x; idle_priority costs nothing until something else
# wants the CPU, so it is the one to keep.
efficiency_mode = {efficiency_mode}
background_io = {background_io}
idle_priority = {idle_priority}
# Hard CPU cap for the indexer, percent of the whole machine (0 = off). Note that on a
# machine with few cores 10% of the whole machine is less than one core.
cpu_rate_percent = {cpu_rate_percent}
# Hard memory cap for the indexer process in MB (0 = off).
memory_limit_mb = {memory_limit_mb}
# After working for t seconds, sleep duty_factor * t seconds.
duty_factor = {duty_factor}
read_mb_per_sec = {read_mb_per_sec}
pause_on_battery = {pause_on_battery}
pause_when_cpu_busy_percent = {pause_when_cpu_busy_percent}
# Index only during these times of day, e.g. ["22:00-06:00"] (a window may pass midnight). Empty = always.
schedule_windows = {schedule_windows}
recycle_after_files = {recycle_after_files}
recycle_rss_mb = {recycle_rss_mb}
# Files are parsed (PDFs are slow) by this many worker processes in parallel, while the indexer
# itself only writes the results to the database. 0 = parse in the indexer, one file at a time.
# Each worker costs some RAM (about 40-100 MB) and, with duty_factor, its own share of CPU;
# cpu_rate_percent caps all of them together and read_mb_per_sec is divided between them.
extract_workers = {extract_workers}
# Writes are batched to reduce SSD wear: the index is committed after this many
# files or seconds, whichever comes first (or when the batch fills the page cache).
# New results appear in search after a commit. A bigger cache allows bigger batches.
commit_every_files = {commit_every_files}
commit_every_seconds = {commit_every_seconds}
sqlite_cache_mb = {sqlite_cache_mb}
wal_autocheckpoint_pages = {wal_autocheckpoint_pages}
usn_poll_seconds = {usn_poll_seconds}
rescan_hours = {rescan_hours}

[storage]
# Folder of index.db (empty = this data folder). It must be on a local drive: SQLite's write-ahead log
# does not work over a network. To move an existing index, stop the service, move index.db* there, set this,
# and start the service; without the files the index is simply rebuilt.
db_dir = {db_dir}

[web]
web_host = {web_host}
web_port = {web_port}
# Protects the pause/resume/reindex and settings actions of the web UI.
token = {token}
"""


def _toml(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)  # a JSON string is a valid TOML basic string
    if isinstance(value, list):
        if len(value) <= 3:
            return "[" + ", ".join(_toml(v) for v in value) + "]"
        lines: list[str] = []  # as many items per line as fit, so a list of 200 extensions is not 200 lines
        for item in (_toml(v) + "," for v in value):
            if lines and len(lines[-1]) + 1 + len(item) <= 100:
                lines[-1] += " " + item
            else:
                lines.append("    " + item)
        return "[\n" + "\n".join(lines) + "\n]"
    return repr(value)


def render(cfg: Config) -> str:
    """config.toml text for `cfg`: the template with every key filled in."""
    values = {f.name: _toml(getattr(cfg, f.name)) for f in fields(Config) if f.name != "data_dir"}
    return CONFIG_TEMPLATE.format_map(values)


def save(cfg: Config) -> None:
    """Rewrite config.toml (keeping the old one as config.toml.bak) without ever leaving a half-written file."""
    path = cfg.data_dir / "config.toml"
    if path.exists():
        shutil.copy2(path, path.with_suffix(".toml.bak"))
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(render(cfg), encoding="utf-8")
    os.replace(tmp, path)


def apply(data_dir: Path, changes: object) -> Config:
    """Validate `changes` and write them into the settings on disk; the file is untouched if any is bad."""
    checked = validate_changes(changes)
    try:
        cfg = load(data_dir)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError({"config.toml": f"cannot be read, fix or delete it first: {e}"}) from e
    for key, value in checked.items():
        setattr(cfg, key, value)
    save(cfg)
    return cfg


def public_settings(cfg: Config) -> dict:
    """What the Settings page shows: current values, what each may be, and the throttle presets."""
    return {
        "values": {key: getattr(cfg, key) for key in SPECS},
        "specs": {key: {"kind": spec.kind.__name__, "lo": spec.lo, "hi": spec.hi, "zero_ok": spec.zero_ok,
                        "items": spec.items} for key, spec in SPECS.items()},
        "presets": THROTTLE_PRESETS,
        "preset": detect_preset(cfg),
    }


def default_data_dir() -> Path:
    if home := os.environ.get("SINDEX_HOME"):
        return Path(home)
    return Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "sindex"


def load(data_dir: Path | None = None) -> Config:
    data_dir = Path(data_dir or default_data_dir()).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "config.toml"
    if not path.exists():
        save(Config(**FRESH_INSTALL, token=secrets.token_urlsafe(24), data_dir=data_dir))

    with path.open("rb") as f:
        raw = tomllib.load(f)
    flat: dict = {}
    for key, value in raw.items():
        if isinstance(value, dict):
            flat.update(value)
        else:
            flat[key] = value

    if "extra_text_extensions" in flat:  # the old way: only what to add to the built-in list
        extra = flat.pop("extra_text_extensions")
        if "text_extensions" not in flat:
            flat["text_extensions"] = sorted(TEXT_EXTS | {e.lower().lstrip(".") for e in extra})
            log.info("config.toml: extra_text_extensions is now part of text_extensions")
        else:
            log.warning("config.toml: extra_text_extensions ignored, text_extensions is already there")
    if "exclude_dir_names" in flat:  # the old way: folder names apart from the patterns
        names = flat.pop("exclude_dir_names")
        globs = flat.get("exclude_globs", [])
        flat["exclude_globs"] = [*names, *(g for g in globs if g.lower() not in {n.lower() for n in names})]
        log.info("config.toml: exclude_dir_names is now part of exclude_globs")
    known = {f.name for f in fields(Config)} - {"data_dir"}
    for key in flat.keys() - known:
        log.warning("unknown config key %r ignored", key)
    cfg = Config(**{k: v for k, v in flat.items() if k in known})
    cfg.data_dir = data_dir
    for window in cfg.schedule_windows:  # a hand-edited typo must not surface later as a crash in the indexer
        try:
            parse_window(window)
        except ValueError as e:
            raise ConfigError({"schedule_windows": f"{window!r} in config.toml {e}"}) from e
    return cfg


def setup_logging(data_dir: Path, name: str, *, console: bool = False) -> None:
    logs = data_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        RotatingFileHandler(logs / f"{name}.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    ]
    if console:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO,
        format=f"%(asctime)s {name}[%(process)d] %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    quiet_third_party_logs()


def quiet_third_party_logs() -> None:
    # pypdf warns about every PDF quirk (a missing fontTools alone once filled the log)
    logging.getLogger("pypdf").setLevel(logging.ERROR)
