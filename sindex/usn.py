"""Volume helpers and the NTFS USN change journal (ctypes, no extra deps).

Reading the journal needs administrator rights (the service runs as LocalSystem).
"""

from __future__ import annotations

import ctypes
import logging
import struct
from collections.abc import Callable, Iterator
from ctypes import wintypes
from dataclasses import dataclass

log = logging.getLogger(__name__)

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

GENERIC_READ = 0x80000000
FILE_SHARE_ALL = 0x1 | 0x2 | 0x4
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
INVALID_HANDLE = wintypes.HANDLE(-1).value
DRIVE_FIXED = 3

FSCTL_QUERY_USN_JOURNAL = 0x000900F4
FSCTL_READ_USN_JOURNAL = 0x000900BB
ERROR_HANDLE_EOF = 38
ERROR_JOURNAL_ENTRY_DELETED = 1181

REASON_DATA = 0x1 | 0x2 | 0x4  # overwrite, extend, truncation
REASON_CREATE = 0x100
REASON_DELETE = 0x200
REASON_RENAME_OLD = 0x1000
REASON_RENAME_NEW = 0x2000
REASON_MASK = REASON_DATA | REASON_CREATE | REASON_DELETE | REASON_RENAME_OLD | REASON_RENAME_NEW

ATTR_DIRECTORY = 0x10

_V2 = struct.Struct("<IHHQQqqIIIIHH")
_V3 = struct.Struct("<IHH16s16sqqIIIIHH")


class JournalError(Exception):
    """Journal failure. `rescan`: the journal stays usable after a full rescan
    (records were purged); otherwise fall back to periodic rescans."""

    def __init__(self, msg: str, *, rescan: bool) -> None:
        super().__init__(msg)
        self.rescan = rescan


class _JournalData(ctypes.Structure):
    _fields_ = [("UsnJournalID", ctypes.c_ulonglong), ("FirstUsn", ctypes.c_longlong),
                ("NextUsn", ctypes.c_longlong), ("LowestValidUsn", ctypes.c_longlong),
                ("MaxUsn", ctypes.c_longlong), ("MaximumSize", ctypes.c_ulonglong),
                ("AllocationDelta", ctypes.c_ulonglong)]


class _ReadData(ctypes.Structure):
    _fields_ = [("StartUsn", ctypes.c_longlong), ("ReasonMask", wintypes.DWORD),
                ("ReturnOnlyOnClose", wintypes.DWORD), ("Timeout", ctypes.c_ulonglong),
                ("BytesToWaitFor", ctypes.c_ulonglong), ("UsnJournalID", ctypes.c_ulonglong)]


class _FileIdUnion(ctypes.Union):
    _fields_ = [("FileId", ctypes.c_longlong), ("ExtendedFileId", ctypes.c_ubyte * 16)]


class _FileIdDescriptor(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("Type", wintypes.DWORD), ("Id", _FileIdUnion)]


kernel32.CreateFileW.restype = wintypes.HANDLE
kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                 wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
kernel32.OpenFileById.restype = wintypes.HANDLE
kernel32.OpenFileById.argtypes = [wintypes.HANDLE, ctypes.POINTER(_FileIdDescriptor), wintypes.DWORD,
                                  wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
kernel32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                     ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                     ctypes.c_void_p]
kernel32.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]


def fixed_drives() -> list[str]:
    buf = ctypes.create_unicode_buffer(512)
    n = kernel32.GetLogicalDriveStringsW(len(buf), buf)
    drives = [d for d in buf[:n].split("\0") if d]
    return [d for d in drives if kernel32.GetDriveTypeW(d) == DRIVE_FIXED]


def fs_type(letter: str) -> str:
    fs = ctypes.create_unicode_buffer(64)
    ok = kernel32.GetVolumeInformationW(letter[0] + ":\\", None, 0, None, None, None, fs, len(fs))
    return fs.value if ok else ""


@dataclass(slots=True)
class Change:
    parent_path: str | None
    name: str
    reason: int
    is_dir: bool

    @property
    def path(self) -> str | None:
        return None if self.parent_path is None else self.parent_path.rstrip("\\") + "\\" + self.name


class Journal:
    BUFFER = 64 * 1024
    PATH_CACHE = 4096

    def __init__(self, letter: str) -> None:
        self.letter = letter[0].upper() + ":"
        self.handle = kernel32.CreateFileW("\\\\.\\" + self.letter, GENERIC_READ, FILE_SHARE_ALL, None,
                                           OPEN_EXISTING, 0, None)
        if self.handle == INVALID_HANDLE or not self.handle:
            raise JournalError(f"open {self.letter}: WinError {ctypes.get_last_error()}", rescan=False)
        self._paths: dict[bytes | int, str | None] = {}

    def close(self) -> None:
        if self.handle:
            kernel32.CloseHandle(self.handle)
            self.handle = None

    def query(self) -> tuple[int, int, int]:
        """(journal_id, lowest_valid_usn, next_usn)."""
        data = _JournalData()
        got = wintypes.DWORD()
        if not kernel32.DeviceIoControl(self.handle, FSCTL_QUERY_USN_JOURNAL, None, 0,
                                        ctypes.byref(data), ctypes.sizeof(data), ctypes.byref(got), None):
            raise JournalError(f"query {self.letter}: WinError {ctypes.get_last_error()}", rescan=False)
        return data.UsnJournalID, data.LowestValidUsn, data.NextUsn

    def _path_by_id(self, file_id: int | bytes) -> str | None:
        if file_id in self._paths:
            return self._paths[file_id]
        desc = _FileIdDescriptor()
        desc.dwSize = ctypes.sizeof(desc)
        if isinstance(file_id, bytes):
            desc.Type = 2  # ExtendedFileIdType
            ctypes.memmove(desc.Id.ExtendedFileId, file_id, 16)
        else:
            desc.Type = 0  # FileIdType
            desc.Id.FileId = ctypes.c_longlong(file_id).value
        path = None
        h = kernel32.OpenFileById(self.handle, ctypes.byref(desc), 0, FILE_SHARE_ALL, None,
                                  FILE_FLAG_BACKUP_SEMANTICS)
        if h and h != INVALID_HANDLE:
            try:
                buf = ctypes.create_unicode_buffer(1024)
                n = kernel32.GetFinalPathNameByHandleW(h, buf, len(buf), 0)
                if n > len(buf):
                    buf = ctypes.create_unicode_buffer(n)
                    n = kernel32.GetFinalPathNameByHandleW(h, buf, len(buf), 0)
                if n:
                    path = buf.value.removeprefix("\\\\?\\")
            finally:
                kernel32.CloseHandle(h)
        if len(self._paths) >= self.PATH_CACHE:
            self._paths.clear()
        self._paths[file_id] = path
        return path

    def read(self, start_usn: int, journal_id: int,
             wanted: Callable[..., bool] = lambda name, reason, *, is_dir: True) -> Iterator[Change]:
        """Yield changes after `start_usn`; `self.next_usn` tracks progress.

        `wanted(name, reason, is_dir=...)` filters records before paths are resolved
        (resolution costs a few system calls).
        """
        self.next_usn = start_usn
        self._paths.clear()
        out = ctypes.create_string_buffer(self.BUFFER)
        got = wintypes.DWORD()
        while True:
            req = _ReadData(self.next_usn, REASON_MASK, 0, 0, 0, journal_id)
            if not kernel32.DeviceIoControl(self.handle, FSCTL_READ_USN_JOURNAL, ctypes.byref(req),
                                            ctypes.sizeof(req), out, self.BUFFER, ctypes.byref(got), None):
                err = ctypes.get_last_error()
                if err == ERROR_HANDLE_EOF:
                    return
                raise JournalError(f"read {self.letter}: WinError {err}",
                                   rescan=err == ERROR_JOURNAL_ENTRY_DELETED)
            size = got.value
            if size <= 8:
                return
            raw = out.raw[:size]
            next_usn = struct.unpack_from("<q", raw, 0)[0]
            offset = 8
            while offset < size:
                length, major = struct.unpack_from("<IH", raw, offset)
                if length == 0:
                    break
                if major in (2, 3):
                    fmt = _V2 if major == 2 else _V3
                    (_, _, _, _, parent, _, _, reason, _, _, attrs, name_len, name_off) = fmt.unpack_from(raw, offset)
                    name = raw[offset + name_off: offset + name_off + name_len].decode("utf-16-le", "replace")
                    is_dir = bool(attrs & ATTR_DIRECTORY)
                    if reason & REASON_MASK and wanted(name, reason, is_dir=is_dir):
                        yield Change(self._path_by_id(parent), name, reason, is_dir)
                offset += length
            self.next_usn = next_usn
