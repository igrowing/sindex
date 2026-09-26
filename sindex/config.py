"""Configuration: defaults, TOML loading and the data directory."""

from __future__ import annotations

import logging
import os
import secrets
import tomllib
from dataclasses import dataclass, field, fields
from logging.handlers import RotatingFileHandler
from pathlib import Path

log = logging.getLogger(__name__)

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
    exclude_dir_names: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE_DIR_NAMES))
    index_hidden: bool = False
    extra_text_extensions: list[str] = field(default_factory=list)
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

    data_dir: Path = field(default=Path("."), repr=False)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "index.db"

    @property
    def text_exts(self) -> frozenset[str]:
        return TEXT_EXTS | {e.lower().lstrip(".") for e in self.extra_text_extensions}

    def builtin_exclude_paths(self) -> list[str]:
        paths = [str(self.data_dir)]
        for var in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData"):
            if value := os.environ.get(var):
                paths.append(value)
        drive = os.environ.get("SystemDrive", "C:")
        paths += [drive + "\\Windows.old", drive + "\\PerfLogs", drive + "\\$WinREAgent"]
        return paths


CONFIG_TEMPLATE = """\
# sindex configuration. Restart the service after editing.

[index]
# Folders to index. Empty = all fixed (non-removable) drives.
roots = []
# Extra folders to skip (Windows, Program Files and ProgramData are always skipped).
exclude_paths = []
index_hidden = false
extra_text_extensions = []
max_text_mb = 50
max_office_mb = 100
max_pdf_mb = 100
max_chars_per_file = 5000000

[throttle]
# How hard the indexer is held back. Each of these slows indexing down; lower/off = faster.
# Process-level switches. On an idle test machine, efficiency_mode alone made indexing about
# 2x slower and background_io about 1.5x; idle_priority costs nothing until something else
# wants the CPU, so it is the one to keep.
efficiency_mode = false
background_io = false
idle_priority = true
# Hard CPU cap for the indexer, percent of the whole machine (0 = off). Note that on a
# machine with few cores 10% of the whole machine is less than one core.
cpu_rate_percent = 0
# Hard memory cap for the indexer process in MB (0 = off).
memory_limit_mb = 512
# After working for t seconds, sleep duty_factor * t seconds.
duty_factor = 0.1
read_mb_per_sec = 0
pause_on_battery = false
pause_when_cpu_busy_percent = 80
recycle_after_files = 5000
recycle_rss_mb = 250
# Files are parsed (PDFs are slow) by this many worker processes in parallel, while the indexer
# itself only writes the results to the database. 0 = parse in the indexer, one file at a time.
# Each worker costs some RAM (about 40-100 MB) and, with duty_factor, its own share of CPU;
# cpu_rate_percent caps all of them together and read_mb_per_sec is divided between them.
extract_workers = 3
# Writes are batched to reduce SSD wear: the index is committed after this many
# files or seconds, whichever comes first (or when the batch fills the page cache).
# New results appear in search after a commit. A bigger cache allows bigger batches.
commit_every_files = 1000
commit_every_seconds = 120
sqlite_cache_mb = 64
wal_autocheckpoint_pages = 4000
usn_poll_seconds = 300
rescan_hours = 24

[web]
web_host = "127.0.0.1"
web_port = 8765
# Protects the pause/resume/reindex actions of the web UI.
token = "{token}"
"""


def default_data_dir() -> Path:
    if home := os.environ.get("SINDEX_HOME"):
        return Path(home)
    return Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "sindex"


def load(data_dir: Path | None = None) -> Config:
    data_dir = Path(data_dir or default_data_dir()).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "config.toml"
    if not path.exists():
        path.write_text(CONFIG_TEMPLATE.format(token=secrets.token_urlsafe(24)), encoding="utf-8")

    with path.open("rb") as f:
        raw = tomllib.load(f)
    flat: dict = {}
    for key, value in raw.items():
        if isinstance(value, dict):
            flat.update(value)
        else:
            flat[key] = value

    known = {f.name for f in fields(Config)} - {"data_dir"}
    for key in flat.keys() - known:
        log.warning("unknown config key %r ignored", key)
    cfg = Config(**{k: v for k, v in flat.items() if k in known})
    cfg.data_dir = data_dir
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
