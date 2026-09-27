r"""Build the Windows executable: dist\sindex\sindex.exe (a folder, see below).

    .venv\Scripts\pip install -e ".[build]"
    .venv\Scripts\python packaging\build.py

It is a folder and not a single file on purpose: the service control manager starts sindex.exe, which must
answer within seconds, and a one-file build would first unpack itself into a temporary folder every time.
"""

import importlib.metadata
from pathlib import Path

import PyInstaller.__main__

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
BUILD_DIR = ROOT / "build"

# version lives only in pyproject.toml (see AGENTS.md); read it the same way the app does at runtime
_version = importlib.metadata.version("sindex")
_version_tuple = tuple(int(part) for part in _version.split("."))[:3] + (0,)

# PyInstaller's --version-file wants this as a Python literal (VSVersionInfo etc. are its own classes);
# without it, sindex.exe shows no product name, version or copyright in Windows Explorer's Properties dialog
_VERSION_INFO = f"""\
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={_version_tuple},
    prodvers={_version_tuple},
    mask=0x3F,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0),
  ),
  kids=[
    StringFileInfo(
      [
        StringTable(
          "040904B0",
          [
            StringStruct("CompanyName", "iGrowing"),
            StringStruct("FileDescription", "sindex content indexer"),
            StringStruct("FileVersion", "{_version}"),
            StringStruct("InternalName", "sindex"),
            StringStruct("LegalCopyright", "Copyright (c) iGrowing"),
            StringStruct("OriginalFilename", "sindex.exe"),
            StringStruct("ProductName", "sindex"),
            StringStruct("ProductVersion", "{_version}"),
          ],
        ),
      ],
    ),
    VarFileInfo([VarStruct("Translation", [1033, 1200])]),
  ],
)
"""

BUILD_DIR.mkdir(exist_ok=True)
_version_info_path = BUILD_DIR / "version_info.txt"
_version_info_path.write_text(_VERSION_INFO, encoding="utf-8")

PyInstaller.__main__.run([
    str(HERE / "sindex_exe.py"),
    "--name", "sindex",
    "--onedir", "--console", "--noconfirm", "--clean",
    "--distpath", str(ROOT / "dist"),
    "--workpath", str(BUILD_DIR),
    "--specpath", str(BUILD_DIR),
    "--paths", str(ROOT),
    "--version-file", str(_version_info_path),
    # the web UI is read from files next to the code
    "--add-data", f"{ROOT / 'sindex' / 'web' / 'static'};sindex/web/static",
    # importlib.metadata.version("sindex") needs the package metadata
    "--copy-metadata", "sindex",
    # the service host, the service classes and the standard library's leftovers we never use
    "--hidden-import", "servicemanager",
    "--hidden-import", "win32timezone",
    "--exclude-module", "tkinter",
    "--exclude-module", "pytest",
    "--exclude-module", "unittest",
])
