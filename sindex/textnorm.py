"""Text normalization before indexing, CJK bigram encoding and chunking.

SQLite's unicode61 tokenizer case-folds and strips Latin diacritics itself, but
keeps Greek tonos, Hebrew niqqud and Arabic harakat, so those are removed here.

CJK text has no spaces, so unicode61 would turn a whole sentence into one token.
Each CJK run is rewritten as overlapping bigrams framed by private-use characters
that the tokenizer is configured to treat as separators:

    東京都  ->  RUN_START 東京 BIGRAM_SEP 京都 RUN_END

The encoding is reversible (see `decode_display`), so snippets read naturally.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Iterator

BIGRAM_SEP = "\ue000"
RUN_START = "\ue001"
RUN_END = "\ue002"
HL_START = "\ue003"  # highlight markers used in snippets
HL_END = "\ue004"

_CJK = (
    "\u1100-\u11ff\u3040-\u309f\u30a0-\u30ff\u3130-\u318f\u31f0-\u31ff"
    "\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff\uff66-\uff9f"
    "\U00020000-\U0002ffff"
)
_CJK_RUN = re.compile(f"[{_CJK}]+")
_CJK_CHAR = re.compile(f"[{_CJK}]")

_GREEK = re.compile("[\u0370-\u03ff\u1f00-\u1fff]")
_GREEK_MARKS = re.compile("([\u0370-\u03ff\u1f00-\u1fff])[\u0300-\u036f]+")
_SEMITIC_MARKS = re.compile(
    "[\u0591-\u05bd\u05bf\u05c1\u05c2\u05c4\u05c5\u05c7"  # Hebrew points and cantillation
    "\u0610-\u061a\u0640\u064b-\u065f\u0670\u06d6-\u06dc\u06df-\u06e4\u06e7\u06e8\u06ea-\u06ed]"  # Arabic
)
_SURROGATE = re.compile("[\ud800-\udfff]")
# Control characters (except tab/newline) and our own private-use markers.
_JUNK = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ufeff\ue000-\ue004]")


def _encode_run(m: re.Match) -> str:
    run = m.group()
    if len(run) == 1:
        return RUN_START + run + RUN_END
    return RUN_START + BIGRAM_SEP.join(run[i:i + 2] for i in range(len(run) - 1)) + RUN_END


def fold(text: str) -> str:
    """Strip marks the tokenizer does not fold; keeps text otherwise readable."""
    if _SURROGATE.search(text):  # pair them up; lone ones cannot be stored as UTF-8
        text = text.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")
    text = _JUNK.sub(" ", text)
    if _GREEK.search(text):
        text = unicodedata.normalize("NFC", _GREEK_MARKS.sub(r"\1", unicodedata.normalize("NFD", text)))
    return _SEMITIC_MARKS.sub("", text)


def normalize(text: str) -> str:
    """Prepare text for storage in the FTS index."""
    return _CJK_RUN.sub(_encode_run, fold(text))


def normalize_query_term(term: str) -> str:
    return normalize(term)


def has_cjk(text: str) -> bool:
    return _CJK_CHAR.search(text) is not None


def decode_display(text: str) -> str:
    """Undo the bigram encoding (highlight markers are passed through)."""
    if RUN_START not in text and BIGRAM_SEP not in text and RUN_END not in text:
        return text
    out = []
    skip = False
    for ch in text:
        if ch == RUN_START or ch == RUN_END:
            skip = False
        elif ch == BIGRAM_SEP:
            skip = True
        elif skip and ch == HL_START:
            # a highlighted bigram also covers the char it shares with the previous one
            if out and out[-1] == HL_END:
                out.pop()
            elif out:
                out.insert(len(out) - 1, HL_START)
            else:
                out.append(ch)
        elif skip and ch != HL_END:
            skip = False  # first char of a bigram repeats the previous bigram's last char
        else:
            out.append(ch)
    return "".join(out)


def chunks(pieces: Iterable[str], size: int, limit: int, info: dict | None = None) -> Iterator[str]:
    """Group raw text pieces into chunks of about `size` chars, cut at whitespace.

    Stops after `limit` characters in total and then sets info["truncated"] if
    any text was left out.
    """
    buf: list[str] = []
    buf_len = 0
    total = 0
    pieces = iter(pieces)
    for piece in pieces:
        if not piece:
            continue
        if total + len(piece) > limit:
            piece = piece[: limit - total]
            if info is not None:
                info["truncated"] = True
        total += len(piece)
        buf.append(piece)
        buf_len += len(piece)
        while buf_len >= size:
            text = "".join(buf)
            cut = _cut_point(text, size)
            yield text[:cut]
            rest = text[cut:]
            buf, buf_len = [rest], len(rest)
        if total >= limit:
            if info is not None and not info.get("truncated"):
                info["truncated"] = any(p for p in pieces)  # anything non-empty left?
            break
    if buf_len:
        text = "".join(buf)
        if text.strip():
            yield text


def _cut_point(text: str, size: int) -> int:
    window = text[max(0, size - 1024):size]
    for sep in ("\n", " ", "\t"):
        pos = window.rfind(sep)
        if pos >= 0:
            return max(0, size - 1024) + pos + 1
    return size
