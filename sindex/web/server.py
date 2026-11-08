"""Local web UI: a threaded stdlib HTTP server bound to localhost.

Reads the index through a read-only SQLite connection opened per request and
controls the indexer only through files (see control.py), so it never
competes with the indexer for the write lock. A slow search must not hold up the
status polling or the next keystroke's search, hence one thread per request,
with at most SEARCH_SLOTS searches running at once.
"""

from __future__ import annotations

import csv
import hmac
import io
import json
import logging
import sqlite3
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from html import unescape as html_unescape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit
from xml.sax.saxutils import escape as xml_escape

import regex

from .. import config, control, db
from ..config import Config
from ..search import Found, find, is_slow, present, search

log = logging.getLogger(__name__)

STATIC = Path(__file__).with_name("static")
PAGES = {"/": "index.html", "/settings": "settings.html", "/log": "log.html"}
MAX_BODY = 64 * 1024
HEALTH_PAGE = 50  # files per page of the health list
RSS_PAGE = 20  # default number of items per page of the Explorer search connector's RSS feed
RSS_PAGE_MAX = 100
SEARCH_SLOTS = 2  # searches that run at the same time; each holds a connection and ranks up to HIT_LIMIT hits
SEARCH_TIMEOUT_S = 30.0  # a query that runs longer is stopped, so an abandoned one cannot hold a slot for ever
# A slow search (search.is_slow: wildcards, case sensitivity, regex) runs in the background with no time limit, its
# page polling what it has found so far; it is stopped when the page stops asking for this long (the tab was closed).
POLL_GRACE_S = 10.0
# the request that starts a slow search waits this long for its first results, so that one which ends (or fails, a
# bad pattern) at once answers without a poll
FIRST_ANSWER_S = 1.0
# slow searches that read text at the same time: one may read the whole index for a minute, and a second would only
# compete with it for the disk
SCAN_SLOTS = 1
FOUND_TTL_S = 120.0  # how long the files a tab found stay available for another sort order or page
FOUND_TABS = 8  # tabs whose search is kept; each holds at most HIT_LIMIT file and chunk numbers (about 100 KB)
# what changes the view of a search, not what it finds
_PAGE_PARAMS = frozenset({"sort", "offset", "tab", "seq", "reuse", "skip_ext"})
_TAB_ID = regex.compile(r"[A-Za-z0-9]{1,32}")
_PROGRESS_OPS = 20_000  # SQLite virtual-machine steps between two looks at the cancel flag
EXPORT_COLUMNS = ("path", "filename", "size_bytes", "modified", "content")
_MARK = regex.compile(r"</?mark>")
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")  # what makes a spreadsheet read a cell as a formula
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
# Anything but letters (of any script), digits, underscore and hyphen is not safe in a file name: Windows refuses
# < > : " / \ | ? *, and ^ ' ` ~ . and spaces cause trouble in shells and at the end of a name.
_UNSAFE_NAME_CHARS = regex.compile(r"[^\p{L}\p{N}_-]+")
EXPORT_NAME_CHARS = 40  # of the search words kept in the file name; a long name is a nuisance in a path of 260


def _cell(text: str) -> str:
    """Text for a CSV cell. The file names and contents come from other people's files: a cell that starts with
    `=`, `+`, `-` or `@` would be run as a formula when the CSV is opened in a spreadsheet, so it gets a quote."""
    return "'" + text if text.startswith(_FORMULA_START) else text


def export_filename(query: str, today: date) -> str:
    """`sindex-<what was searched>-<date>.csv`: every run of unsafe characters in the query becomes one underscore.
    A query with nothing usable left (empty, or only symbols; a search by photo and video filters has no words) is
    called "results"."""
    words = _UNSAFE_NAME_CHARS.sub("_", query).strip("_-")[:EXPORT_NAME_CHARS].rstrip("_-")
    return f"sindex-{words or 'results'}-{today.isoformat()}.csv"


def _local_time(seconds: float) -> str:
    try:
        return datetime.fromtimestamp(seconds).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        # Windows has no local time for dates before 1970; say it is UTC rather than pass it off as local
        return (_EPOCH + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def results_csv(results: list[dict]) -> str:
    """The files of a search as CSV: folder, file name, size in bytes, date and time of the file (local time) and the
    text that the page shows for the file, without its highlighting. Files found by name only have no text."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(EXPORT_COLUMNS)
    for r in results:
        content = " ".join(html_unescape(_MARK.sub("", r["snippet"])).split())
        writer.writerow([_cell(r["dir"]), _cell(r["name"]), r["size"], _local_time(r["mtime"]), _cell(content)])
    return out.getvalue()


class SearchTicket:
    """One running search: it is stopped by a newer search of the same browser tab or when it runs too long."""

    __slots__ = ("cancelled", "deadline", "seq", "timed_out")

    def __init__(self, seq: int, timeout_s: float) -> None:
        self.seq = seq
        self.cancelled = False
        self.timed_out = False
        self.deadline = time.monotonic() + timeout_s

    @property
    def stopped(self) -> bool:
        return self.cancelled or self.timed_out

    def should_stop(self) -> int:
        """SQLite progress handler: a non-zero return aborts the running statement."""
        if not self.cancelled and time.monotonic() > self.deadline:
            self.timed_out = True
        return 1 if self.stopped else 0

    def extend(self, seconds: float) -> None:
        """A page still wants this search: it may run `seconds` longer from now."""
        self.deadline = max(self.deadline, time.monotonic() + seconds)


class SearchRegistry:
    """Latest search wins: starting search `seq` of a tab cancels that tab's older running search."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current: dict[str, SearchTicket] = {}

    def begin(self, tab: str | None, seq: int, timeout_s: float = SEARCH_TIMEOUT_S) -> SearchTicket:
        ticket = SearchTicket(seq, timeout_s)
        if tab is None:
            return ticket
        with self._lock:
            old = self._current.get(tab)
            if old is not None and old.seq > seq:
                ticket.cancelled = True  # a request that overtook this one is already registered
                return ticket
            if old is not None:
                old.cancelled = True
            self._current[tab] = ticket
        return ticket

    def cancel(self, tab: str, seq: int) -> None:
        """Stop the tab's running search if it is `seq` or older (a newer one is not what the page gave up on)."""
        with self._lock:
            ticket = self._current.get(tab)
            if ticket is not None and ticket.seq <= seq:
                ticket.cancelled = True

    def end(self, tab: str | None, ticket: SearchTicket) -> None:
        if tab is None:
            return
        with self._lock:
            if self._current.get(tab) is ticket:
                del self._current[tab]


def _extension_set(text: str) -> frozenset[str]:
    """"pdf,TXT,." -> {"pdf", "txt", ""}: the page writes "no extension" as a dot, the one thing no extension is."""
    return frozenset("" if item == "." else item.lower() for item in text.split(",") if item)


def _search_key(q: dict) -> tuple:
    """What a search found depends on these parameters; the others only change how it is shown."""
    return tuple(sorted((k, v) for k, v in q.items() if k not in _PAGE_PARAMS))


def _find_options(q: dict) -> dict:
    """search.find() arguments, besides the query, from the request parameters. ValueError: a bad number."""
    exts = [e for e in q.get("ext", "").replace(",", " ").split() if e]
    return {
        "path": q.get("path") or None, "exts": exts or None,
        "after": q.get("after") or None, "before": q.get("before") or None, "names": q.get("names") == "1",
        "regex": q.get("regex") == "1", "case_sensitive": q.get("case") == "1",
        "kind": q.get("kind") or None, "camera": q.get("camera") or None,
        "has_gps": {"1": True, "0": False}.get(q.get("has_gps")),
        "min_width": int(q["min_width"]) if q.get("min_width") else None,
        "min_height": int(q["min_height"]) if q.get("min_height") else None,
        "min_duration_s": float(q["min_duration"]) if q.get("min_duration") else None,
        "max_duration_s": float(q["max_duration"]) if q.get("max_duration") else None,
        "taken_after": q.get("taken_after") or None, "taken_before": q.get("taken_before") or None,
    }


def _view_options(q: dict, *, export: bool) -> dict:
    """search.present() arguments from the request parameters: the order, the page, the excluded extensions."""
    return {
        "sort": q.get("sort") or "relevance", "limit": None if export else 20,
        "offset": 0 if export else max(0, int(q.get("offset", 0) or 0)),
        "skip": _extension_set(q.get("skip_ext", "")) or frozenset(),
    }


def _background_search(server: _Server, q: dict, tab: str, kept: KeptSearch, first: threading.Event) -> None:
    """Run a slow search, putting what it has found so far into `kept` for the page's polls; `first` is set when
    there is something to show (or the search ended). A failed or stopped search leaves the cache, with its error."""
    ticket = kept.ticket
    assert ticket is not None, "a background search has a ticket"

    def update(found: Found) -> None:
        kept.found = found
        first.set()

    try:
        while not server.scan_slots.acquire(timeout=0.5):  # another tab's slow search is reading the index
            if ticket.should_stop():
                return
        try:
            conn = db.connect(server.cfg.db_path, readonly=True)
            try:
                conn.set_progress_handler(ticket.should_stop, _PROGRESS_OPS)
                kept.found = find(conn, q.get("query", ""), **_find_options(q), progress=update)
            finally:
                conn.close()
        finally:
            server.scan_slots.release()
    except ValueError as e:  # SearchError: a bad pattern, nothing to search for; or a filter that is not a number
        kept.error = str(e)
    except sqlite3.OperationalError:
        if not ticket.stopped:
            log.exception("search %r failed", q.get("query", ""))
            kept.error = "The search failed; the log has the details."
    except Exception:
        # a thread's exception would otherwise only reach stderr, and the page would poll a search that never ends
        log.exception("search %r failed", q.get("query", ""))
        kept.error = "The search failed; the log has the details."
    finally:
        if ticket.stopped:
            kept.error = "The search was stopped."  # never shown as if it had found everything
        kept.running = False
        server.searches.end(tab, ticket)
        if kept.error is None:
            server.found.finished(tab, kept)
        else:
            server.found.discard(tab, kept)
        first.set()


@dataclass(slots=True)
class KeptSearch:
    """A search of a tab: what it found, and while it still runs in the background (`ticket` is set), what it has
    found so far. Its thread replaces `found` with a newer snapshot; a reader takes the reference once."""

    found: Found
    ticket: SearchTicket | None = None
    running: bool = False
    error: str | None = None


class FoundCache:
    """The last search of each browser tab, so that sorting it again or turning a page does not read all the text
    again. An entry is for one set of search words and filters (`key`) and ends `ttl_s` after the search finished:
    the index changes."""

    def __init__(self, ttl_s: float = FOUND_TTL_S, tabs: int = FOUND_TABS) -> None:
        self._ttl_s = ttl_s
        self._tabs = tabs
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, tuple[tuple, float, KeptSearch]] = OrderedDict()

    def get(self, tab: str, key: tuple) -> KeptSearch | None:
        with self._lock:
            entry = self._entries.get(tab)
            if entry is None or entry[0] != key or (not entry[2].running and time.monotonic() > entry[1]):
                return None
            return entry[2]

    def put(self, tab: str, key: tuple, kept: KeptSearch) -> None:
        with self._lock:
            self._entries[tab] = (key, time.monotonic() + self._ttl_s, kept)
            self._entries.move_to_end(tab)
            while len(self._entries) > self._tabs:
                self._entries.popitem(last=False)

    def finished(self, tab: str, kept: KeptSearch) -> None:
        """A background search ended: its time to live starts now, unless the tab has searched something else."""
        with self._lock:
            entry = self._entries.get(tab)
            if entry is not None and entry[2] is kept:
                self._entries[tab] = (entry[0], time.monotonic() + self._ttl_s, kept)

    def discard(self, tab: str, kept: KeptSearch) -> None:
        """A background search failed or was stopped: it is not kept (a newer search of the tab stays)."""
        with self._lock:
            entry = self._entries.get(tab)
            if entry is not None and entry[2] is kept:
                del self._entries[tab]


class _Handler(BaseHTTPRequestHandler):
    server_version = "sindex v." + config.VERSION
    server: _Server

    def log_message(self, fmt, *args) -> None:
        log.debug("%s " + fmt, self.address_string(), *args)

    # -- helpers -------------------------------------------------------

    def _send(self, code: int, body: bytes, ctype: str, headers: dict[str, str] | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, code: int = 200) -> None:
        self._send(code, json.dumps(data).encode("utf-8"), "application/json; charset=utf-8")

    def _host_ok(self) -> bool:
        # rejects DNS-rebinding requests that reach us under a foreign host name
        port = self.server.cfg.web_port
        return self.headers.get("Host", "") in {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}

    def _connect(self) -> sqlite3.Connection | None:
        path = self.server.cfg.db_path
        return db.connect(path, readonly=True) if path.exists() else None

    # -- routes ----------------------------------------------------------

    def do_GET(self) -> None:
        if not self._host_ok():
            return self._send(403, b"forbidden", "text/plain")
        url = urlsplit(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path in PAGES:
            page = (STATIC / PAGES[url.path]).read_text(encoding="utf-8")
            page = page.replace("{{TOKEN}}", json.dumps(self.server.cfg.token)).replace("{{VERSION}}", config.VERSION)
            if "{{COMPLETE_AFTER}}" in page:
                after = config.load(self.server.cfg.data_dir).search_complete_after_s  # as saved, no restart needed
                page = page.replace("{{COMPLETE_AFTER}}", json.dumps(after))
            return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
        if url.path == "/style.css":
            return self._send(200, (STATIC / "style.css").read_bytes(), "text/css; charset=utf-8")
        if url.path == "/api/settings":
            return self._json(config.public_settings(config.load(self.server.cfg.data_dir)))
        if url.path == "/api/health":
            return self._health(query)
        if url.path == "/api/browse":
            return self._browse(query)
        if url.path == "/api/log":
            return self._log(query)
        if url.path == "/api/search":
            return self._search(query)
        if url.path == "/api/export.csv":
            return self._export(query)
        if url.path == "/api/search/cancel":
            return self._cancel_search(query)
        if url.path == "/api/status":
            return self._status()
        if url.path == "/opensearch.xml":
            return self._opensearch()
        if url.path == "/api/search.rss":
            return self._search_rss(query)
        self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:
        with self.server.post_lock:
            self._post()

    def _post(self) -> None:
        # the body is read before anything is refused: closing a connection with unread request data makes
        # Windows reset it, and the client then sees a connection error instead of the 403
        length = min(int(self.headers.get("Content-Length") or 0), MAX_BODY)
        raw = self.rfile.read(length)
        if not self._host_ok():
            return self._send(403, b"forbidden", "text/plain")
        token = self.headers.get("X-Sindex-Token", "")
        if not self.server.cfg.token or not hmac.compare_digest(token, self.server.cfg.token):
            return self._json({"error": "bad token"}, 403)
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            return self._json({"error": "bad json"}, 400)
        if not isinstance(body, dict):
            return self._json({"error": "bad json"}, 400)
        path = urlsplit(self.path).path
        cfg = self.server.cfg
        if path == "/api/pause":
            control.pause(cfg)
        elif path == "/api/resume":
            control.resume(cfg)
        elif path == "/api/settings":
            return self._change_settings(body)
        elif path == "/api/retry" and body.get("status") in control.RETRY_STATUSES:
            control.request_retry(cfg, body["status"])
        elif path == "/api/rebuild":
            control.request_rebuild(cfg)
        elif path == "/api/maintenance" and body.get("action") in control.MAINTENANCE_ACTIONS:
            control.request_maintenance(cfg, body["action"])
        elif path == "/api/reindex" and isinstance(body.get("path"), str) and body["path"].strip():
            control.request_reindex(cfg, body["path"].strip())
        elif (path == "/api/open" and isinstance(body.get("path"), str) and body["path"].strip()
              and body.get("target", "file") in ("file", "folder")):
            return self._open_file(body["path"].strip(), folder=body.get("target") == "folder")
        else:
            return self._json({"error": "unknown action"}, 404)
        self._json({"ok": True})

    def _open_file(self, path: str, *, folder: bool = False) -> None:
        # only shows something when sindex runs interactively; the installed service has no desktop to use
        # (LocalSystem in Session 0), so this fails there instead of silently doing nothing
        try:
            (control.open_folder if folder else control.open_file)(path)
        except (OSError, ValueError) as e:
            return self._json({"error": f"could not open {path}: {e}"}, 400)
        self._json({"ok": True})

    def _change_settings(self, body: dict) -> None:
        # validated here so that the UI hears about a bad value at once; the supervisor writes the file
        try:
            changes = config.validate_changes(body.get("changes"), check_paths=True)
        except config.ConfigError as e:
            return self._json({"error": str(e), "errors": e.errors}, 400)
        control.request_settings(self.server.cfg, changes)
        self._json({"ok": True})

    def _token_ok(self) -> bool:
        token = self.headers.get("X-Sindex-Token", "")
        return bool(self.server.cfg.token) and hmac.compare_digest(token, self.server.cfg.token)

    def _browse(self, q: dict) -> None:
        # the folder names of the whole computer are not for every web page that can reach localhost
        if not self._token_ok():
            return self._json({"error": "bad token"}, 403)
        try:
            self._json(control.list_folders(q.get("path", "")))
        except ValueError as e:
            self._json({"error": str(e)}, 400)

    def _log(self, q: dict) -> None:
        # logs name the files and folders that were indexed
        if not self._token_ok():
            return self._json({"error": "bad token"}, 403)
        try:
            self._json(control.read_log(self.server.cfg, q.get("name", ""), int(q.get("kb") or 256)))
        except ValueError as e:
            self._json({"error": str(e)}, 400)

    def _health(self, q: dict) -> None:
        conn = self._connect()
        if conn is None:
            return self._json({"counts": {}, "files": [], "limit": HEALTH_PAGE})
        try:
            status = q.get("status")
            files = []
            if status:
                files = control.problem_files(conn, status, limit=HEALTH_PAGE, offset=max(0, int(q.get("offset") or 0)))
            counts = control.index_stats(conn, self.server.cfg)["files"]
        except ValueError as e:
            return self._json({"error": str(e)}, 400)
        finally:
            conn.close()
        self._json({"counts": {s: counts.get(s, 0) for s in control.RETRY_STATUSES}, "files": files,
                    "limit": HEALTH_PAGE})

    def _search(self, q: dict) -> None:
        self._json(self._search_result(q, export=False))

    def _export(self, q: dict) -> None:
        """Every file of the search the page shows (all pages, its sort and extension menus) as a CSV download.

        The page sends the same parameters as for the search, so the tab's kept search is used and nothing is
        read again. UTF-8 with a byte order mark: without it a spreadsheet reads the text as the ANSI code page.
        """
        result = self._search_result(q, export=True)
        if result.get("error"):
            return self._json(result, 400)
        name = export_filename(q.get("query", ""), datetime.now().date())
        # RFC 5987: the name may hold any letters, and a header is Latin-1 only
        disposition = f"attachment; filename*=UTF-8''{quote(name)}"
        self._send(200, results_csv(result["results"]).encode("utf-8-sig"), "text/csv; charset=utf-8",
                   {"Content-Disposition": disposition})

    def _search_result(self, q: dict, *, export: bool) -> dict:
        tab = q["tab"] if _TAB_ID.fullmatch(q.get("tab", "")) else None
        key = _search_key(q)
        kept = self.server.found.get(tab, key) if tab and q.get("reuse") == "1" else None
        if kept is not None and kept.running:
            # the page polls a search that runs in the background: what it has found so far
            if export:
                return {"results": [], "total": 0, "error": "The search is still running; export it when it ends."}
            assert kept.ticket is not None, "a running search has a ticket"
            kept.ticket.extend(POLL_GRACE_S)
            return self._present_kept(q, kept)
        if kept is None and tab and not export and is_slow(q.get("query", ""), regex=q.get("regex") == "1",
                                                            case_sensitive=q.get("case") == "1"):
            if not self.server.cfg.db_path.exists():
                return {"results": [], "total": 0, "error": "The index does not exist yet."}
            try:
                seq = int(q.get("seq", 0) or 0)
            except ValueError as e:
                return {"results": [], "total": 0, "error": str(e)}
            kept = self._start_in_background(q, tab, key, seq)
            if kept is None:
                return {"results": [], "total": 0, "capped": False, "error": None, "cancelled": True}
            return self._present_kept(q, kept)

        conn = self._connect()
        if conn is None:
            return {"results": [], "total": 0, "error": "The index does not exist yet."}
        searches = self.server.searches
        try:
            # an export is not a newer search of the tab: it must not cancel the one the page is waiting for
            ticket = searches.begin(None if export else tab, int(q.get("seq", 0) or 0))
        except ValueError as e:
            conn.close()
            return {"results": [], "total": 0, "error": str(e)}
        result: dict | None = None
        try:
            with self.server.search_slots:  # waiting here is cheap: a newer search may cancel this one meanwhile
                if not ticket.cancelled:
                    conn.set_progress_handler(ticket.should_stop, _PROGRESS_OPS)
                    result = self._run_search(conn, q, tab, key, kept, export=export)
        except ValueError as e:
            result = {"results": [], "total": 0, "error": str(e)}
        except sqlite3.OperationalError:
            if not ticket.stopped:
                raise
        finally:
            searches.end(None if export else tab, ticket)
            conn.close()
        if ticket.cancelled:
            result = {"results": [], "total": 0, "capped": False, "error": None, "cancelled": True}
        elif ticket.timed_out:
            result = {"results": [], "total": 0, "capped": False,
                      "error": f"The search took longer than {SEARCH_TIMEOUT_S:.0f} s and was stopped. Try more words."}
        assert result is not None, "a search that was not stopped has a result"
        return result

    def _cancel_search(self, q: dict) -> None:
        # the page calls this when its user edits the search box: closing the request does not stop the query
        # inside SQLite, so the server is told
        if _TAB_ID.fullmatch(q.get("tab", "")):
            try:
                self.server.searches.cancel(q["tab"], int(q.get("seq", 0) or 0))
            except ValueError:
                return self._json({"error": "bad seq"}, 400)
        self._json({})

    def _run_search(self, conn: sqlite3.Connection, q: dict, tab: str | None, key: tuple, kept: KeptSearch | None,
                    *, export: bool = False) -> dict:
        """The page of results for `q`, or with `export` all of them, from the tab's kept search (`reuse=1`:
        another sort or page of the same words and filters) or, without one, from searching the text."""
        if kept is None:
            kept = KeptSearch(find(conn, q.get("query", ""), **_find_options(q)))
            if tab:
                self.server.found.put(tab, key, kept)
        if kept.error is not None:
            return {"results": [], "total": 0, "capped": False, "error": kept.error}
        return {**present(conn, kept.found, **_view_options(q, export=export)), "running": False}

    def _start_in_background(self, q: dict, tab: str, key: tuple, seq: int) -> KeptSearch | None:
        """Start a slow search of the tab on a thread of its own and wait a moment for its first results: a search
        that ends at once, or fails at once (a bad pattern), answers in one go. None: a newer search of the tab
        is already running."""
        server = self.server
        ticket = server.searches.begin(tab, seq, timeout_s=POLL_GRACE_S)
        if ticket.cancelled:
            return None
        kept = KeptSearch(Found.empty(), ticket=ticket, running=True)
        server.found.put(tab, key, kept)
        first = threading.Event()
        # daemon: the thread only reads the index, so ending it with the process loses nothing
        threading.Thread(target=_background_search, args=(server, q, tab, kept, first), name=f"search-{tab}",
                         daemon=True).start()
        first.wait(FIRST_ANSWER_S)
        return kept

    def _present_kept(self, q: dict, kept: KeptSearch) -> dict:
        # `running` before `found`: the thread sets its final `found` before it says it stopped running
        running = kept.running
        found = kept.found
        if kept.error is not None:
            return {"results": [], "total": 0, "capped": False, "error": kept.error}
        conn = self._connect()
        if conn is None:
            return {"results": [], "total": 0, "error": "The index does not exist yet."}
        try:
            return {**present(conn, found, **_view_options(q, export=False)), "running": running}
        except ValueError as e:
            return {"results": [], "total": 0, "error": str(e)}
        finally:
            conn.close()

    def _status(self) -> None:
        cfg = self.server.cfg
        data = {"indexer": control.indexer_status(cfg)}
        sup = self.server.supervisor
        if sup is not None:
            data["supervisor"] = {"running": sup.running, "restarts": sup.restarts, "last_exit": sup.last_exit,
                                  "settings_error": sup.settings_error}
        conn = self._connect()
        if conn is not None:
            try:
                data["index"] = control.index_stats(conn, cfg)
            finally:
                conn.close()
        self._json(data)

    def _opensearch(self) -> None:
        # the OpenSearch description Explorer reads from the .searchConnector-ms file (see
        # cli.create_search_connector); it points Explorer's search box at _search_rss below
        cfg = self.server.cfg
        base = f"http://{cfg.web_host}:{cfg.web_port}"
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<OpenSearchDescription xmlns="http://a9.com/-/spec/opensearch/1.1/">\n'
            "  <ShortName>sindex</ShortName>\n"
            "  <Description>Search file contents indexed by sindex</Description>\n"
            f'  <Url type="application/rss+xml" template="{base}/api/search.rss?'
            'query={searchTerms}&amp;count={count?}&amp;startIndex={startIndex?}"/>\n'
            "</OpenSearchDescription>\n"
        )
        self._send(200, body.encode("utf-8"), "application/opensearchdescription+xml; charset=utf-8")

    def _rss_item(self, r: dict) -> str:
        link = "file:///" + quote(r["path"].replace("\\", "/"), safe="/:")
        snippet = regex.sub(r"</?mark>", "", html_unescape(r["snippet"])) if r["snippet"] else ""
        modified = datetime.fromtimestamp(r["mtime"], tz=UTC).strftime("%a, %d %b %Y %H:%M:%S GMT")
        return (
            f"<item><title>{xml_escape(r['name'])}</title><link>{link}</link>"
            f"<description>{xml_escape(snippet)}</description>"
            f"<A:DateModified>{modified}</A:DateModified><A:Size>{r['size']}</A:Size></item>"
        )

    def _search_rss(self, q: dict) -> None:
        # results for the Explorer search connector, as an RSS feed of file:// links Explorer can open,
        # preview and right-click like any other file; see _opensearch above for the query template
        conn = self._connect()
        try:
            start = max(1, int(q.get("startIndex") or 1))
            count = max(1, min(int(q.get("count") or RSS_PAGE), RSS_PAGE_MAX))
        except ValueError as e:
            return self._send(400, f"bad startIndex/count: {e}".encode(), "text/plain")
        results: list[dict] = []
        total = 0
        if conn is not None:
            try:
                # Explorer asks on every keystroke: the index-only answer, never the scan of the stored text
                result = search(conn, q.get("query", ""), offset=start - 1, limit=count, scan=False)
            finally:
                conn.close()
            results, total = result["results"], result["total"]
        items = "".join(self._rss_item(r) for r in results)
        ns = 'xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/"'
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<rss version="2.0" xmlns:A="http://schemas.microsoft.com/windows/2008/propertynamespace" {ns}>\n'
            "<channel><title>sindex search</title>"
            f"<opensearch:totalResults>{total}</opensearch:totalResults>"
            f"<opensearch:startIndex>{start}</opensearch:startIndex>"
            f"<opensearch:itemsPerPage>{count}</opensearch:itemsPerPage>"
            f"{items}</channel></rss>\n"
        )
        self._send(200, body.encode("utf-8"), "application/rss+xml; charset=utf-8")


class _Server(ThreadingHTTPServer):
    # a request thread only reads the index or drops a control file (atomically), so ending it with the
    # process loses nothing; non-daemon threads would hold the service open during a long search
    daemon_threads = True

    def __init__(self, cfg: Config, supervisor) -> None:
        self.cfg = cfg
        self.supervisor = supervisor
        self.searches = SearchRegistry()
        self.found = FoundCache()
        self.search_slots = threading.BoundedSemaphore(SEARCH_SLOTS)
        self.scan_slots = threading.BoundedSemaphore(SCAN_SLOTS)
        # POSTs change settings and control files; they ran one at a time before the server was threaded
        self.post_lock = threading.Lock()
        config.check_web_host(cfg.web_host)  # again: tests and callers may build a Config without config.load()
        super().__init__((cfg.web_host, cfg.web_port), _Handler)
        host = self.server_address[0]
        if host != cfg.web_host:  # defence in depth: the socket must be where the configuration says
            self.server_close()
            raise config.ConfigError({"web_host": f"the server is bound to {host}, not to {cfg.web_host}"})

    def handle_error(self, request, client_address) -> None:
        # the page aborts a search the moment the next keystroke supersedes it, which the server meets as a
        # reset connection while it writes the answer; that is expected, anything else is logged in full
        if isinstance(sys.exception(), ConnectionError):
            log.debug("client %s went away", client_address)
        else:
            log.exception("web request from %s failed", client_address)


class WebServer:
    def __init__(self, cfg: Config, supervisor=None) -> None:
        self.cfg = cfg
        self.supervisor = supervisor
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        try:
            self._server = _Server(self.cfg, self.supervisor)
        except (OSError, config.ConfigError) as e:
            log.error("web UI unavailable on %s:%d: %s", self.cfg.web_host, self.cfg.web_port, e)
            return
        self._thread = threading.Thread(target=self._server.serve_forever, name="web", daemon=True)
        self._thread.start()
        log.info("web UI on http://%s:%d/", self.cfg.web_host, self.cfg.web_port)

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
