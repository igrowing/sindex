import json
import socket
import threading
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import pytest

from sindex import config
from sindex.extract import Context
from sindex.throttle import Throttle
from sindex.web.server import WebServer


@pytest.fixture
def cfg(tmp_path: Path) -> config.Config:
    c = config.load(tmp_path / "data")
    c.duty_factor = 0
    c.read_mb_per_sec = 0
    c.pause_on_battery = False
    c.pause_when_cpu_busy_percent = 0
    c.extract_workers = 0  # parse in the test process; the tests of the worker pool ask for processes
    return c


@pytest.fixture
def web(cfg):
    """The web UI on a free port, with the token "secret"."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        cfg.web_port = s.getsockname()[1]
    cfg.token = "secret"
    server = WebServer(cfg)
    server.start()
    yield cfg
    server.stop()


def call(cfg, path: str, body: dict | list | None = None, token: str | None = None):
    """(status, body bytes) of a GET, or of a POST when `body` is given."""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Sindex-Token"] = token
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(f"http://127.0.0.1:{cfg.web_port}{path}", data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


@pytest.fixture
def ctx(cfg) -> Context:
    return Context(Throttle(cfg, threading.Event()))


def make_zip(path: Path, members: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in members.items():
            zf.writestr(name, content)
    return path


W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
A = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'


def make_docx(path: Path, paragraphs: list[str]) -> Path:
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    return make_zip(path, {
        "[Content_Types].xml": "<Types/>",
        "word/document.xml": f'<?xml version="1.0" encoding="UTF-8"?><w:document {W}><w:body>{body}'
                             f'<w:p><w:r><w:instrText>PAGE</w:instrText></w:r></w:p></w:body></w:document>',
    })


def make_xlsx(path: Path, strings: list[str]) -> Path:
    items = "".join(f"<si><t>{s}</t></si>" for s in strings)
    return make_zip(path, {
        "xl/sharedStrings.xml": '<?xml version="1.0" encoding="UTF-8"?><sst xmlns='
                                f'"http://schemas.openxmlformats.org/spreadsheetml/2006/main">{items}</sst>',
    })


def make_pptx(path: Path, slides: list[str]) -> Path:
    members = {}
    for i, text in enumerate(slides, 1):
        members[f"ppt/slides/slide{i}.xml"] = (
            f'<?xml version="1.0" encoding="UTF-8"?><p:sld {A} '
            'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
            f"<p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>{text}</a:t></a:r></a:p>"
            "</p:txBody></p:sp></p:spTree></p:cSld></p:sld>")
    return make_zip(path, members)


def make_odt(path: Path, paragraphs: list[str]) -> Path:
    t = 'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"'
    o = 'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"'
    body = "".join(f"<text:p>{p}</text:p>" for p in paragraphs)
    return make_zip(path, {
        "mimetype": "application/vnd.oasis.opendocument.text",
        "content.xml": f'<?xml version="1.0" encoding="UTF-8"?><office:document-content {o} {t}>'
                       f"<office:font-face-decls>IGNORED</office:font-face-decls>"
                       f"<office:body><office:text>{body}</office:text></office:body></office:document-content>",
    })


def make_encrypted_pdf(path: Path, lines: list[str], algorithm: str, user_password: str = "") -> Path:
    """An encrypted copy of make_pdf(). An empty user password is the common 'permissions only' kind."""
    from pypdf import PdfReader, PdfWriter

    plain = make_pdf(path.with_name("plain-" + path.name), lines)
    writer = PdfWriter(clone_from=PdfReader(plain))
    writer.encrypt(user_password=user_password, owner_password="owner-secret", algorithm=algorithm)
    with path.open("wb") as f:
        writer.write(f)
    return path


def make_pdf(path: Path, lines: list[str]) -> Path:
    """A minimal valid PDF with one page of Helvetica text (ASCII only)."""
    ops = b"BT /F1 18 Tf 72 720 Td " + b" 0 -24 Td ".join(
        b"(" + line.encode("latin-1") + b") Tj" for line in lines) + b" ET"
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(ops) + ops + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, obj in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % off for off in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    path.write_bytes(out)
    return path
