import codecs

import pypdf
import pytest
from pypdf.errors import DependencyError

from sindex.extract import ExtractError, NotText, extract
from sindex.extract.text import detect_encoding

from .conftest import make_docx, make_encrypted_pdf, make_odt, make_pdf, make_pptx, make_xlsx, make_zip

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
