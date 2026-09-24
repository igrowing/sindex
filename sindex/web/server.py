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

from .. import control, db
from ..config import Config
from ..search import search

log = logging.getLogger(__name__)

STATIC = Path(__file__).with_name("static")
MAX_BODY = 64 * 1024


class _Handler(BaseHTTPRequestHandler):
    server_version = "sindex"
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
        if url.path == "/":
            page = (STATIC / "index.html").read_text(encoding="utf-8")
            page = page.replace("{{TOKEN}}", json.dumps(self.server.cfg.token))
            return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
        if url.path == "/api/search":
            return self._search(q)
        if url.path == "/api/status":
            return self._status()
        self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:
        if not self._host_ok():
            return self._send(403, b"forbidden", "text/plain")
        token = self.headers.get("X-Sindex-Token", "")
        if not self.server.cfg.token or not hmac.compare_digest(token, self.server.cfg.token):
            return self._json({"error": "bad token"}, 403)
        length = min(int(self.headers.get("Content-Length") or 0), MAX_BODY)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._json({"error": "bad json"}, 400)
        path = urlsplit(self.path).path
        cfg = self.server.cfg
        if path == "/api/pause":
            control.pause(cfg)
        elif path == "/api/resume":
            control.resume(cfg)
        elif path == "/api/reindex" and isinstance(body.get("path"), str) and body["path"].strip():
            control.request_reindex(cfg, body["path"].strip())
        else:
            return self._json({"error": "unknown action"}, 404)
        self._json({"ok": True})

    def _search(self, q: dict) -> None:
        conn = self._connect()
        if conn is None:
            return self._json({"results": [], "total": 0, "error": "The index does not exist yet."})
        try:
            exts = [e for e in q.get("ext", "").replace(",", " ").split() if e]
            result = search(conn, q.get("q", ""), path=q.get("path") or None, exts=exts or None,
                            after=q.get("after") or None, before=q.get("before") or None,
                            offset=max(0, int(q.get("offset", 0) or 0)), limit=20)
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
            data["supervisor"] = {"running": sup.running, "restarts": sup.restarts, "last_exit": sup.last_exit}
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
