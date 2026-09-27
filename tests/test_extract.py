import codecs

import pypdf
import pytest
from pypdf.errors import DependencyError

from sindex.extract import ExtractError, NotText, extract
from sindex.extract.text import detect_encoding

from .conftest import (
    FIXTURES,
    make_docx,
    make_encrypted_pdf,
    make_jpeg,
    make_jpeg_with_iptc,
    make_odt,
    make_pdf,
    make_png,
    make_pptx,
    make_xlsx,
    make_zip,
)

RU = ("Съешь же ещё этих мягких французских булок, да выпей чаю. Широкая электрификация южных "
      "губерний даст мощный толчок подъёму сельского хозяйства. В чащах юга жил бы цитрус? Да, но фальшивый "
      "экземпляр! Москва - столица России, а Киев - столица Украины. ")
EL = ("Ξεσκεπάζω την ψυχοφθόρα βδελυγμία. Τάχιστη αλώπηξ βαφής ψημένη γη, δρασκελίζει υπέρ νωθρού "
      "κυνός. Η Αθήνα είναι η πρωτεύουσα της Ελλάδας και η Θεσσαλονίκη η δεύτερη μεγαλύτερη πόλη. ")
HE = ("דג סקרן שט בים מאוכזב ולפתע מצא חברה. עטלף אבק נס דרך מזגן שהתפוצץ כי חם. ירושלים היא "
      "עיר עתיקה ותל אביב היא עיר על חוף הים התיכון. ")
AR = ("نص حكيم له سر قاطع وذو شأن عظيم مكتوب على ثوب أخضر ومغلف بجلد أزرق. القاهرة هي عاصمة مصر "
      "والرياض هي عاصمة المملكة العربية السعودية. ")
LATIN = ("Voix ambiguë d'un cœur qui, au zéphyr, préfère les jattes de kiwis. Falsches Üben von "
         "Xylophonmusik quält jeden größeren Zwerg. Quel vituperabile xenofobo zelante assaggia il whisky ed "
         "esclama: alleluja! Élève, garçon, forêt, Straße, perché. ")
JA = ("いろはにほへと ちりぬるを わかよたれそ つねならむ。日本語の文章を正しく検出できるかどうかを確認します。"
      "東京都は日本の首都です。カタカナとひらがなと漢字が混ざっています。")
ZH_S = "我能吞下玻璃而不伤身体。中文字符编码检测测试，北京是中国的首都，上海是最大的城市，这是一个简体中文的例子。"
ZH_T = "我能吞下玻璃而不傷身體。台北是台灣最大的城市，這是一個繁體中文的例子，用來測試編碼檢測是否正確。"
KO = "다람쥐 헌 쳇바퀴에 타고파. 한국어 인코딩 감지 테스트입니다. 서울은 대한민국의 수도이며 부산은 항구 도시입니다."

CASES = [
    (RU, "cp1251", "французских"), (RU, "koi8_r", "французских"), (RU, "cp866", "французских"),
    (RU, "utf-8", "французских"), (RU, "utf-16", "французских"), (RU, "utf-8-sig", "французских"),
    (EL, "cp1253", "Ελλάδας"), (EL, "iso8859_7", "Ελλάδας"),
    (HE, "cp1255", "ירושלים"),
    (AR, "cp1256", "القاهرة"),
    (LATIN, "cp1252", "größeren"), (LATIN.replace("œ", "oe"), "latin-1", "zéphyr"),
    (JA, "shift_jis", "東京都"), (JA, "euc_jp", "東京都"),
    (ZH_S, "gb18030", "北京"), (ZH_T, "big5", "台北"),
    (KO, "euc_kr", "서울은"),
]


@pytest.mark.parametrize("text,encoding,needle", CASES, ids=[c[1] + "-" + c[2] for c in CASES])
def test_text_encodings(tmp_path, ctx, text, encoding, needle):
    p = tmp_path / "sample.txt"
    p.write_bytes((text * 3).encode(encoding))
    out = "".join(extract(str(p), "text", ctx))
    assert needle in out, f"detected {ctx.encoding}"


def test_utf16_without_bom(tmp_path, ctx):
    p = tmp_path / "log.txt"
    p.write_bytes(("plain ASCII log line with some words\r\n" * 40).encode("utf-16-le"))
    out = "".join(extract(str(p), "text", ctx))
    assert "ASCII log line" in out and ctx.encoding == "utf-16-le"


def test_utf8_split_across_sample_boundary():
    data = ("я" * 40000).encode("utf-8")[: 64 * 1024 - 1]  # cut inside a 2-byte char
    assert detect_encoding(data, complete=False) == "utf-8"


def test_binary_is_rejected(tmp_path, ctx):
    p = tmp_path / "blob.txt"
    p.write_bytes(bytes(range(256)) * 50)
    with pytest.raises(NotText):
        list(extract(str(p), "text", ctx))


def test_large_file_is_streamed(tmp_path, ctx):
    p = tmp_path / "big.log"
    line = "строка журнала номер %d\n"
    p.write_text("".join(line % i for i in range(60000)), encoding="cp1251")
    pieces = list(extract(str(p), "text", ctx))
    assert len(pieces) > 5  # arrived in blocks, not in one piece
    assert "номер 59999" in "".join(pieces)


def test_html_declared_charset_and_tags(tmp_path, ctx):
    p = tmp_path / "page.html"
    html = ('<html><head><meta charset="windows-1251"><style>.x{color:red}</style>'
            "<script>var hidden = 1;</script></head><body><p>Привет, мир</p><p>второй абзац</p></body></html>")
    p.write_bytes(html.encode("cp1251"))
    out = "".join(extract(str(p), "text", ctx))
    assert "Привет, мир" in out and "второй" in out
    assert "hidden" not in out and "color" not in out and "<p>" not in out


def test_docx(tmp_path, ctx):
    p = make_docx(tmp_path / "a.docx", ["Первый абзац", "Zweiter Absatz", "東京都"])
    out = "".join(extract(str(p), "ooxml", ctx))
    assert "Первый абзац\n" in out and "Zweiter Absatz" in out and "東京都" in out
    assert "PAGE" not in out  # field codes are not text


def test_xlsx(tmp_path, ctx):
    p = make_xlsx(tmp_path / "a.xlsx", ["Ячейка", "Κελί", "cell"])
    out = "".join(extract(str(p), "ooxml", ctx))
    assert out.split() == ["Ячейка", "Κελί", "cell"]


def test_pptx(tmp_path, ctx):
    p = make_pptx(tmp_path / "a.pptx", ["Слайд один", "Slide two"])
    out = "".join(extract(str(p), "ooxml", ctx))
    assert "Слайд один" in out and "Slide two" in out


def test_odt(tmp_path, ctx):
    p = make_odt(tmp_path / "a.odt", ["Bonjour à tous", "שלום עולם"])
    out = "".join(extract(str(p), "odf", ctx))
    assert "Bonjour à tous" in out and "שלום עולם" in out and "IGNORED" not in out


def test_office_doctype_rejected(tmp_path, ctx):
    p = make_zip(tmp_path / "evil.docx", {
        "word/document.xml": '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><x>&a;</x>'})
    with pytest.raises(ExtractError):
        list(extract(str(p), "ooxml", ctx))


def test_not_a_zip(tmp_path, ctx):
    p = tmp_path / "broken.docx"
    p.write_bytes(b"this is not a zip file at all")
    with pytest.raises(ExtractError):
        list(extract(str(p), "ooxml", ctx))


def test_rtf_code_pages_and_unicode(tmp_path, ctx):
    rtf = (r"{\rtf1\ansi\ansicpg1252\deff0{\fonttbl{\f0\fswiss Arial;}{\f1\fcharset204 Arial Cyr;}"
           r"{\f2\fcharset161 Arial Greek;}}{\colortbl;\red0\green0\blue0;}"
           r"{\*\generator Riched20;}\pard\f0 Hello caf\'e9\par"
           r"\f1 \'cf\'f0\'e8\'e2\'e5\'f2\par"
           r"\f2 \'e1\'e5\'e9\'e1\par"
           "\\f0\\uc1\\u26481?\\u20140?\\u-10179?\\u-8704?\\par"  # 東京 and U+1F600 as a surrogate pair
           r"{\pict\wmetafile8 0123456789abcdef}{\*\unknowndest secret}Done\par}")
    p = tmp_path / "a.rtf"
    p.write_bytes(rtf.encode("ascii"))
    out = "".join(extract(str(p), "rtf", ctx))
    assert "Hello café" in out
    assert "Привет" in out
    assert "αεια" in out
    assert "東京" in out
    assert "\ud83d" in out  # surrogate pair halves, joined later by textnorm
    assert "Done" in out
    assert "secret" not in out and "0123456789" not in out and "Arial" not in out


def test_rtf_split_across_blocks(tmp_path, ctx):
    body = r"\'cf\'f0\'e8\'e2\'e5\'f2 " * 20000  # longer than one 64 KB block
    rtf = r"{\rtf1\ansi\ansicpg1251 " + body + "}"
    p = tmp_path / "big.rtf"
    p.write_bytes(rtf.encode("ascii"))
    out = "".join(extract(str(p), "rtf", ctx))
    assert out.count("Привет") == 20000


def test_pdf(tmp_path, ctx):
    p = make_pdf(tmp_path / "a.pdf", ["Hello PDF world", "second line"])
    out = "".join(extract(str(p), "pdf", ctx))
    assert "Hello PDF world" in out and "second line" in out


def test_broken_pdf(tmp_path, ctx):
    p = tmp_path / "bad.pdf"
    p.write_bytes(b"%PDF-1.4\ngarbage")
    with pytest.raises(ExtractError):
        list(extract(str(p), "pdf", ctx))


class _PdfWithoutCrypto:
    """Stands in for a PdfReader that hits an AES-encrypted PDF without `cryptography` installed."""

    is_encrypted = False
    fail_at = "open"

    def __init__(self, *args, **kwargs):
        if self.fail_at == "open":
            raise DependencyError(CRYPTO_MESSAGE)

    @property
    def pages(self):
        if self.fail_at == "count":
            raise DependencyError(CRYPTO_MESSAGE)
        return [self]

    def extract_text(self):
        raise DependencyError(CRYPTO_MESSAGE)


CRYPTO_MESSAGE = "cryptography>=3.1 is required for AES algorithm"


class _Page:
    def __init__(self, reader, text=None, error=None):
        self.reader, self.text, self.error = reader, text, error

    def extract_text(self):
        # what pypdf does: everything decoded for a page is cached on the reader for good
        self.reader.cache_seen.append(len(self.reader.resolved_objects))
        self.reader.resolved_objects[len(self.reader.cache_seen)] = object()
        if self.error:
            raise self.error
        return self.text


class _Reader:
    is_encrypted = False

    def __init__(self, plan):
        self.resolved_objects = {}
        self.cache_seen = []
        self.pages = [_Page(self, *item) for item in plan]


def _use_reader(monkeypatch, reader):
    monkeypatch.setattr(pypdf, "PdfReader", lambda *args, **kwargs: reader)


def test_pdf_page_memory_is_released_after_every_page(tmp_path, ctx, monkeypatch):
    # pypdf keeps every decoded font and stream for the life of the reader, so memory used to
    # grow with the page count: a 124-page PDF peaked at 47 MB, with releasing it is 22 MB
    reader = _Reader([("one",), ("two",), ("three",)])
    _use_reader(monkeypatch, reader)
    out = "".join(extract(str(make_pdf(tmp_path / "a.pdf", ["x"])), "pdf", ctx))
    assert out.split() == ["one", "two", "three"]
    assert reader.cache_seen == [0, 0, 0], "each page must start with an empty object cache"


def test_pdf_page_that_runs_out_of_memory_is_skipped(tmp_path, ctx, monkeypatch):
    # a single page can need >100 MB to decode; under the memory cap that is a MemoryError.
    # It costs that page, not the whole document.
    reader = _Reader([("one",), (None, MemoryError()), ("three",)])
    _use_reader(monkeypatch, reader)
    out = "".join(extract(str(make_pdf(tmp_path / "a.pdf", ["x"])), "pdf", ctx))
    assert out.split() == ["one", "three"]


@pytest.mark.parametrize("algorithm", ["RC4-128", "AES-128", "AES-256"])
def test_pdf_encrypted_with_empty_user_password_is_readable(tmp_path, ctx, algorithm):
    # the usual manufacturer datasheet: opens without a password but has an owner password and
    # permission flags. AES needs a crypto library, which is a project dependency.
    p = make_encrypted_pdf(tmp_path / "datasheet.pdf", ["BME280 pressure sensor", "second line"], algorithm)
    out = "".join(extract(str(p), "pdf", ctx))
    assert "BME280 pressure sensor" in out and "second line" in out


def test_pdf_needing_a_real_password_is_an_extract_error(tmp_path, ctx):
    p = make_encrypted_pdf(tmp_path / "locked.pdf", ["secret"], "AES-256", user_password="user-secret")
    with pytest.raises(ExtractError):
        list(extract(str(p), "pdf", ctx))


@pytest.mark.parametrize("fail_at", ["open", "count", "page"])
def test_pdf_missing_crypto_dependency_is_an_extract_error(tmp_path, ctx, monkeypatch, fail_at):
    # pypdf's DependencyError is a plain Exception, not a PyPdfError: it used to escape as an
    # "unexpected error" with a full traceback instead of a clean, explained status
    reader = type("Reader", (_PdfWithoutCrypto,), {"fail_at": fail_at})
    monkeypatch.setattr(pypdf, "PdfReader", reader)
    p = make_pdf(tmp_path / "encrypted.pdf", ["x"])
    with pytest.raises(ExtractError, match="cryptography"):
        list(extract(str(p), "pdf", ctx))


def test_codecs_used_exist():
    for _, enc, _ in CASES:
        codecs.lookup(enc)


# --- image metadata (Phase 2) ------------------------------------------------------------------

def test_image_with_no_metadata_is_ok_with_no_text(tmp_path, ctx):
    p = make_jpeg(tmp_path / "plain.jpg", size=(12, 9))
    out = "".join(extract(str(p), "image", ctx))
    assert out == ""
    assert ctx.media.kind == "image"
    assert (ctx.media.width, ctx.media.height) == (12, 9)


@pytest.mark.parametrize("lat,lon", [(40.6892, -74.0445), (-33.8688, 151.2093)])
def test_image_exif_camera_date_and_gps(tmp_path, ctx, lat, lon):
    exif = {271: "Canon", 272: "EOS R5", 36867: "2023:06:01 10:00:00"}
    p = make_jpeg(tmp_path / "photo.jpg", exif=exif, gps=(lat, lon))
    list(extract(str(p), "image", ctx))
    media = ctx.media
    assert media.camera == "Canon EOS R5"
    assert media.taken_at == "2023-06-01T10:00:00"
    assert media.lat == pytest.approx(lat, abs=1e-4)
    assert media.lon == pytest.approx(lon, abs=1e-4)


def test_image_exif_user_comment_and_xp_keywords_non_latin(tmp_path, ctx):
    cyrillic = "Отпуск в горах"
    exif = {
        37510: b"UNICODE\x00" + "café at noon".encode("utf-16"),
        40094: cyrillic.encode("utf-16-le") + b"\x00\x00",
    }
    p = make_jpeg(tmp_path / "tagged.jpg", exif=exif)
    out = "".join(extract(str(p), "image", ctx))
    assert "café at noon" in out
    assert cyrillic in out


def test_image_xmp_title_description_subject_are_searchable(tmp_path, ctx):
    xmp = (
        b'<?xpacket begin=""?><x:xmpmeta xmlns:x="adobe:ns:meta/">'
        b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        b'<rdf:Description xmlns:dc="http://purl.org/dc/elements/1.1/">'
        b'<dc:title><rdf:Alt><rdf:li xml:lang="x-default">\xe6\x9d\xb1\xe4\xba\xac\xe9\x83\xbd'
        b"</rdf:li></rdf:Alt></dc:title>"
        b"<dc:subject><rdf:Bag><rdf:li>vacation</rdf:li><rdf:li>mountains</rdf:li></rdf:Bag></dc:subject>"
        b"</rdf:Description></rdf:RDF></x:xmpmeta><?xpacket end=\"w\"?>"
    )
    p = make_jpeg(tmp_path / "xmp.jpg", xmp=xmp)
    out = "".join(extract(str(p), "image", ctx))
    assert "東京都" in out
    assert "vacation" in out and "mountains" in out


def test_image_iptc_caption_headline_byline_keywords_non_latin(tmp_path, ctx):
    greek = "Ελλάδα"
    p = make_jpeg_with_iptc(tmp_path / "iptc.jpg", caption="A caption", headline="A headline",
                             byline="By Someone", keywords=["one", greek])
    out = "".join(extract(str(p), "image", ctx))
    assert "A caption" in out and "A headline" in out and "By Someone" in out
    assert "one" in out and greek in out


def test_png_text_chunks_are_searchable(tmp_path, ctx):
    p = make_png(tmp_path / "pic.png", text={"Title": "A PNG title", "Keywords": "alpha beta"})
    out = "".join(extract(str(p), "image", ctx))
    assert "A PNG title" in out and "alpha beta" in out
    assert ctx.media.kind == "image"


def test_image_never_calls_load(tmp_path, ctx, monkeypatch):
    from PIL import Image

    def boom(self):
        raise AssertionError("image metadata extraction must never decode pixels")

    p = make_jpeg(tmp_path / "photo.jpg", exif={271: "Canon"})
    monkeypatch.setattr(Image.Image, "load", boom)
    list(extract(str(p), "image", ctx))  # must not raise


def test_corrupt_image_is_an_extract_error(tmp_path, ctx):
    p = tmp_path / "broken.jpg"
    p.write_bytes(b"not an image at all" * 5)
    with pytest.raises(ExtractError):
        list(extract(str(p), "image", ctx))


def test_dng_raw_is_read_as_tiff_exif(tmp_path, ctx):
    # DNG is a TIFF-based container; a plain TIFF stands in for it (Pillow reads both the same way).
    p = make_jpeg(tmp_path / "raw.dng", exif={271: "Adobe", 272: "DNG Converter"}, size=(6, 5))
    from PIL import Image

    img = Image.open(p)
    tiff_path = tmp_path / "as_tiff.tiff"
    img.save(tiff_path, format="TIFF", exif=img.getexif())
    list(extract(str(tiff_path), "image", ctx))
    assert (ctx.media.width, ctx.media.height) == (6, 5)
    assert ctx.media.camera == "Adobe DNG Converter"


@pytest.mark.parametrize("ext", ["cr2", "nef", "arw"])
def test_unparseable_raw_is_an_extract_error_not_a_crash(tmp_path, ctx, ext):
    p = tmp_path / f"broken.{ext}"
    p.write_bytes(b"\x00" * 32 + b"not a real raw file")
    with pytest.raises(ExtractError):
        list(extract(str(p), "image", ctx))


# --- video metadata (Phase 2) -------------------------------------------------------------------

def test_video_duration_resolution_codec_and_title(ctx):
    out = "".join(extract(str(FIXTURES / "sample.mp4"), "video", ctx))
    media = ctx.media
    assert (media.width, media.height) == (16, 16)
    assert media.duration_s == pytest.approx(0.04, abs=0.05)
    assert media.codec
    assert "Test Video" in out and "A test comment" in out


def test_video_gps_from_quicktime_xyz_atom(ctx):
    list(extract(str(FIXTURES / "sample_gps.mov"), "video", ctx))
    assert ctx.media.lat == pytest.approx(40.6892, abs=1e-4)
    assert ctx.media.lon == pytest.approx(-74.0445, abs=1e-4)


def test_video_uses_the_capped_parse_speed(ctx, monkeypatch):
    # parse_speed must stay capped: raising it toward 1.0 trades the header-only guarantee away
    from pymediainfo import MediaInfo

    seen = {}
    real_parse = MediaInfo.parse.__func__

    def spy(cls, filename, **kwargs):
        seen.update(kwargs)
        return real_parse(cls, filename, **kwargs)

    monkeypatch.setattr(MediaInfo, "parse", classmethod(spy))
    list(extract(str(FIXTURES / "sample.mp4"), "video", ctx))
    assert seen.get("parse_speed") == pytest.approx(0.3)


def test_video_missing_media_info_library_is_an_extract_error(ctx, monkeypatch):
    from pymediainfo import MediaInfo

    monkeypatch.setattr(MediaInfo, "can_parse", staticmethod(lambda: False))
    with pytest.raises(ExtractError):
        list(extract(str(FIXTURES / "sample.mp4"), "video", ctx))
