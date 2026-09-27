"""Local web UI: a single-threaded stdlib HTTP server bound to localhost.

Reads the index through a read-only SQLite connection opened per request and
controls the indexer only through files (see control.py), so it never
competes with the indexer for the write lock.
"""

from __future__ import annotations

import hmac
import json
import logging
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .. import config, control, db
from ..config import Config
from ..search import search

log = logging.getLogger(__name__)

STATIC = Path(__file__).with_name("static")
PAGES = {"/": "index.html", "/settings": "settings.html", "/log": "log.html"}
MAX_BODY = 64 * 1024
HEALTH_PAGE = 50  # files per page of the health list


class _Handler(BaseHTTPRequestHandler):
    server_version = "sindex v." + config.VERSION
    server: _Server

    def log_message(self, fmt, *args) -> None:
        log.debug("%s " + fmt, self.address_string(), *args)

    # -- helpers -------------------------------------------------------

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
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
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path in PAGES:
            page = (STATIC / PAGES[url.path]).read_text(encoding="utf-8")
            page = page.replace("{{TOKEN}}", json.dumps(self.server.cfg.token)).replace("{{VERSION}}", config.VERSION)
            return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
        if url.path == "/style.css":
            return self._send(200, (STATIC / "style.css").read_bytes(), "text/css; charset=utf-8")
        if url.path == "/api/settings":
            return self._json(config.public_settings(config.load(self.server.cfg.data_dir)))
        if url.path == "/api/health":
            return self._health(q)
        if url.path == "/api/browse":
            return self._browse(q)
        if url.path == "/api/log":
            return self._log(q)
        if url.path == "/api/search":
            return self._search(q)
        if url.path == "/api/status":
            return self._status()
        self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:
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
        elif path == "/api/open" and isinstance(body.get("path"), str) and body["path"].strip():
            return self._open_file(body["path"].strip())
        else:
            return self._json({"error": "unknown action"}, 404)
        self._json({"ok": True})

    def _open_file(self, path: str) -> None:
        # only shows something when sindex runs interactively; the installed service has no desktop to use
        # (LocalSystem in Session 0), so this fails there instead of silently doing nothing
        try:
            control.open_file(path)
        except OSError as e:
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
        conn = self._connect()
        if conn is None:
            return self._json({"results": [], "total": 0, "error": "The index does not exist yet."})
        try:
            exts = [e for e in q.get("ext", "").replace(",", " ").split() if e]
            result = search(
                conn, q.get("q", ""), path=q.get("path") or None, exts=exts or None,
                after=q.get("after") or None, before=q.get("before") or None,
                offset=max(0, int(q.get("offset", 0) or 0)), limit=20, names=q.get("names") == "1",
                kind=q.get("kind") or None, camera=q.get("camera") or None,
                has_gps={"1": True, "0": False}.get(q.get("has_gps")),
                min_width=int(q["min_width"]) if q.get("min_width") else None,
                min_height=int(q["min_height"]) if q.get("min_height") else None,
                min_duration_s=float(q["min_duration"]) if q.get("min_duration") else None,
                max_duration_s=float(q["max_duration"]) if q.get("max_duration") else None,
                taken_after=q.get("taken_after") or None, taken_before=q.get("taken_before") or None)
        except ValueError as e:
            result = {"results": [], "total": 0, "error": str(e)}
        finally:
            conn.close()
        self._json(result)

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


class _Server(HTTPServer):
    daemon_threads = True

    def __init__(self, cfg: Config, supervisor) -> None:
        self.cfg = cfg
        self.supervisor = supervisor
        super().__init__((cfg.web_host, cfg.web_port), _Handler)


class WebServer:
    def __init__(self, cfg: Config, supervisor=None) -> None:
        self.cfg = cfg
        self.supervisor = supervisor
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        try:
            self._server = _Server(self.cfg, self.supervisor)
        except OSError as e:
            log.error("web UI unavailable on %s:%d: %s", self.cfg.web_host, self.cfg.web_port, e)
            return
        self._thread = threading.Thread(target=self._server.serve_forever, name="web", daemon=True)
        self._thread.start()
        log.info("web UI on http://%s:%d/", self.cfg.web_host, self.cfg.web_port)

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
