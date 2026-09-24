import pytest

from sindex import textnorm
from sindex.textnorm import BIGRAM_SEP, HL_END, HL_START, decode_display, fold, normalize


@pytest.mark.parametrize("text", [
    "東京都に住んでいます",
    "Python東京x",
    "漢 字",
    "한국어 텍스트",
    "mixed 中文 and English 日本語",
    "單",
])
def test_cjk_round_trip(text):
    assert decode_display(normalize(text)) == fold(text)


def test_cjk_bigrams():
    assert normalize("東京都") == "\ue001東京\ue000京都\ue002"
    assert normalize("東") == "\ue001東\ue002"


def test_highlight_survives_decoding():
    encoded = normalize("東京都")
    # highlight the second bigram as FTS5 snippet() would
    marked = encoded.replace("京都", HL_START + "京都" + HL_END)
    assert decode_display(marked) == "東" + HL_START + "京都" + HL_END
    # two adjacent highlighted bigrams merge into one highlight
    both = encoded.replace("東京", HL_START + "東京" + HL_END)
    both = both.replace(BIGRAM_SEP + "京都", BIGRAM_SEP + HL_START + "京都" + HL_END)
    assert decode_display(both) == HL_START + "東京都" + HL_END


def test_greek_accents_removed():
    assert fold("καφές Ελληνικά ΐ") == "καφες Ελληνικα ι"


def test_latin_accents_kept_for_display():
    assert fold("Café naïve Straße") == "Café naïve Straße"


def test_hebrew_and_arabic_marks_removed():
    assert fold("ש\u05b8\u05c1לו\u05b9ם") == "שלום"
    assert fold("م\u064eر\u0652ح\u064eب\u064bا") == "مرحبا"
    assert fold("كـــتاب") == "كتاب"  # tatweel


def test_junk_and_markers_neutralized():
    assert fold("a\x00b\ue000c\ufeff") == "a b c "


def test_lone_surrogates_are_repaired():
    high, low = chr(0xD83D), chr(0xDE00)  # the two halves of U+1F600 as separate code units
    assert fold("x" + high + low + "y") == "x😀y"
    assert "\ud800" not in fold("x\ud800y")


def test_chunks_split_on_whitespace_and_respect_limit():
    words = [f"word{i}" for i in range(1000)]
    pieces = [" ".join(words[i:i + 7]) + " " for i in range(0, 1000, 7)]
    out = list(textnorm.chunks(pieces, size=200, limit=10_000))
    assert all(len(c) <= 200 for c in out)
    assert all(c.endswith((" ", "\n")) for c in out[:-1])
    assert "".join(out) == "".join(pieces)  # nothing lost below the limit
    limited = list(textnorm.chunks(pieces, size=200, limit=500))
    assert sum(map(len, limited)) == 500
