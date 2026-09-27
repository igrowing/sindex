import json
import socket
import struct
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

# tests/fixtures/sample.mp4 and sample_gps.mov are tiny (~1.5 KB) real videos, not hand-rolled bytes:
# MP4/MOV box nesting makes a minimal-bytes builder impractical the way make_pdf() is for PDF. Produced with:
#   ffmpeg -f lavfi -i color=c=blue:s=16x16:d=0.04 -metadata title="Test Video" \
#     -metadata comment="A test comment" -metadata artist="Test Camera" -metadata date="2023-06-01T10:00:00" \
#     -pix_fmt yuv420p -frames:v 1 sample.mp4
#   ffmpeg -f lavfi -i color=c=green:s=16x16:d=0.04 -metadata location="+40.6892-074.0445/" \
#     -pix_fmt yuv420p -frames:v 1 -f mov sample_gps.mov
FIXTURES = Path(__file__).parent / "fixtures"


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


def _gps_ifd(lat: float, lon: float) -> dict[int, object]:
    def dms(deg: float) -> tuple[float, float, float]:
        deg = abs(deg)
        d = int(deg)
        m = int((deg - d) * 60)
        s = (deg - d - m / 60) * 3600
        return (float(d), float(m), s)

    return {1: "N" if lat >= 0 else "S", 2: dms(lat), 3: "E" if lon >= 0 else "W", 4: dms(lon)}


def make_jpeg(path: Path, *, exif: dict[int, object] | None = None, gps: tuple[float, float] | None = None,
              xmp: bytes | None = None, size: tuple[int, int] = (8, 8)) -> Path:
    """A tiny JPEG, optionally carrying EXIF tags, a GPS IFD (lat, lon) and/or an XMP packet."""
    from PIL import Image

    img = Image.new("RGB", size, "red")
    kwargs: dict = {}
    if exif or gps:
        e = img.getexif()
        for tag, value in (exif or {}).items():
            e[tag] = value
        if gps:
            gps_ifd = e.get_ifd(0x8825)
            gps_ifd.update(_gps_ifd(*gps))
        kwargs["exif"] = e
    if xmp is not None:
        kwargs["xmp"] = xmp
    img.save(path, format="JPEG", **kwargs)
    return path


def make_jpeg_with_iptc(path: Path, *, caption: str | None = None, headline: str | None = None,
                         byline: str | None = None, keywords: list[str] | None = None) -> Path:
    """A tiny JPEG with an IPTC/IIM block in a Photoshop APP13 segment (Pillow cannot write IPTC itself)."""
    from PIL import Image

    def iptc_field(dataset: int, text: str) -> bytes:
        data = text.encode("utf-8")
        return bytes([0x1C, 2, dataset]) + struct.pack(">H", len(data)) + data

    fields = b""
    if caption:
        fields += iptc_field(120, caption)
    if headline:
        fields += iptc_field(105, headline)
    if byline:
        fields += iptc_field(80, byline)
    for kw in keywords or []:
        fields += iptc_field(25, kw)

    resource = b"8BIM" + struct.pack(">H", 0x0404) + b"\x00\x00" + struct.pack(">I", len(fields)) + fields
    if len(fields) % 2:
        resource += b"\x00"
    app13 = b"\xff\xed" + struct.pack(">H", len(resource) + 16) + b"Photoshop 3.0\x00" + resource

    buf = path.with_suffix(".tmp")
    Image.new("RGB", (8, 8), "red").save(buf, format="JPEG")
    raw = buf.read_bytes()
    buf.unlink()
    path.write_bytes(raw[:2] + app13 + raw[2:])
    return path


def make_png(path: Path, *, text: dict[str, str] | None = None, size: tuple[int, int] = (8, 8)) -> Path:
    """A tiny PNG, optionally with tEXt metadata (Title/Description/Author/Keywords)."""
    from PIL import Image
    from PIL.PngImagePlugin import PngInfo

    info = PngInfo()
    for key, value in (text or {}).items():
        info.add_text(key, value)
    Image.new("RGB", size, "green").save(path, format="PNG", pnginfo=info)
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
