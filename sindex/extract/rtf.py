"""RTF: small streaming tokenizer that keeps text and drops everything else."""

from __future__ import annotations

import codecs
import re
from collections.abc import Iterator

from . import Context, ExtractError

BLOCK_BYTES = 64 * 1024

_TOKEN = re.compile(
    r"\\([a-zA-Z]{1,32})(-?\d{1,10})? ?"  # control word
    r"|\\'([0-9a-fA-F]{2})"  # hex-escaped byte
    r"|\\(.)"  # control symbol
    r"|([{}])"
    r"|([^\\{}\r\n]+)"  # plain text run
    r"|[\r\n]+",
    re.DOTALL,
)

# destinations whose content is not document text
_SKIP_DESTINATIONS = frozenset("""
    fonttbl colortbl stylesheet listtable listoverridetable info pict object objdata themedata
    colorschememapping datastore latentstyles rsidtbl xmlnstbl generator mmathPr fldinst
    filetbl revtbl protusertbl bkmkstart bkmkend xe tc datafield fldtype pn pntext pntxtb pntxta
""".split())

# \fcharset -> Windows code page
_CHARSET_CP = {0: 1252, 161: 1253, 162: 1254, 163: 1258, 177: 1255, 178: 1256, 186: 1257,
               204: 1251, 238: 1250, 222: 874, 128: 932, 134: 936, 136: 950, 129: 949}


def _codec(cp: int) -> str:
    name = f"cp{cp}"
    try:
        codecs.lookup(name)
        return name
    except LookupError:
        return "cp1252"


class _State:
    __slots__ = ("skip", "uc", "font", "in_fonttbl")

    def __init__(self, *, skip: bool = False, uc: int = 1, font: int | None = None, in_fonttbl: bool = False):
        self.skip, self.uc, self.font, self.in_fonttbl = skip, uc, font, in_fonttbl

    def copy(self) -> _State:
        return _State(skip=self.skip, uc=self.uc, font=self.font, in_fonttbl=self.in_fonttbl)


class _Rtf:
    def __init__(self) -> None:
        self.out: list[str] = []
        self.pending = bytearray()  # consecutive \'hh bytes, decoded together (CJK)
        self.stack: list[_State] = []
        self.state = _State()
        self.default_cp = 1252
        self.font_cp: dict[int, int] = {}
        self.cur_font_def: int | None = None
        self.skip_chars = 0  # fallback chars to skip after \uN
        self.group_start = False  # directly after '{' (for \* detection)

    def _cp(self) -> int:
        return self.font_cp.get(self.state.font, self.default_cp)

    def flush_bytes(self) -> None:
        if self.pending:
            self.out.append(self.pending.decode(_codec(self._cp()), errors="replace"))
            self.pending.clear()

    def emit(self, text: str) -> None:
        if self.state.skip:
            return
        if self.skip_chars:
            drop = min(self.skip_chars, len(text))
            self.skip_chars -= drop
            text = text[drop:]
        if text:
            self.flush_bytes()
            self.out.append(text)

    def feed_token(self, m: re.Match) -> None:
        word, arg, hexbyte, symbol, brace, text = m.groups()
        at_group_start, self.group_start = self.group_start, False
        if brace == "{":
            self.flush_bytes()
            self.stack.append(self.state.copy())
            self.group_start = True
        elif brace == "}":
            self.flush_bytes()
            if self.stack:
                self.state = self.stack.pop()
        elif hexbyte is not None:
            if self.state.skip:
                return
            if self.skip_chars:
                self.skip_chars -= 1
                return
            self.pending.append(int(hexbyte, 16))
        elif symbol is not None:
            if symbol == "*" and at_group_start:
                self.state.skip = True
            elif symbol in "\\{}":
                self.emit(symbol)
            elif symbol == "~":
                self.emit("\u00a0")
            elif symbol == "_":
                self.emit("-")
            self.group_start = symbol == "*" and at_group_start
        elif word is not None:
            self.control_word(word, int(arg) if arg else None, at_group_start=at_group_start)
        elif text is not None:
            if self.state.in_fonttbl or self.state.skip:
                return
            # raw 8-bit characters are bytes in the current code page
            if any(ord(c) > 127 for c in text):
                self.emit(text.encode("latin-1").decode(_codec(self._cp()), errors="replace"))
            else:
                self.emit(text)

    def control_word(self, word: str, arg: int | None, *, at_group_start: bool) -> None:
        st = self.state
        if word in _SKIP_DESTINATIONS:
            if word == "fonttbl":
                st.in_fonttbl = True
            else:
                st.skip = True
            return
        if st.in_fonttbl:
            if word == "f" and arg is not None:
                self.cur_font_def = arg
            elif word == "fcharset" and arg is not None and self.cur_font_def is not None:
                if arg in _CHARSET_CP:
                    self.font_cp[self.cur_font_def] = _CHARSET_CP[arg]
            elif word == "cpg" and arg is not None and self.cur_font_def is not None:
                self.font_cp[self.cur_font_def] = arg
            return
        if word == "ansicpg" and arg:
            self.default_cp = arg
        elif word == "f" and arg is not None:
            self.flush_bytes()
            st.font = arg
        elif word == "uc" and arg is not None:
            st.uc = arg
        elif word == "u" and arg is not None:
            if not st.skip:
                self.emit(chr(arg + 65536 if arg < 0 else arg))
                self.skip_chars = st.uc
        elif word in ("par", "line", "sect", "page", "row"):
            self.emit("\n")
        elif word in ("tab", "cell"):
            self.emit("\t")
        elif word in ("emdash", "endash"):
            self.emit("\u2014" if word == "emdash" else "\u2013")
        elif word in ("lquote", "rquote"):
            self.emit("'")
        elif word in ("ldblquote", "rdblquote"):
            self.emit('"')
        elif word == "bin" and arg:
            raise ExtractError("RTF with \\bin data outside skipped groups is not supported")

    def take(self) -> str:
        text = "".join(self.out)
        self.out.clear()
        return text


def extract(f, ctx: Context) -> Iterator[str]:
    head = f.read(5)
    if head != b"{\\rtf":
        raise ExtractError("not an RTF file")
    rtf = _Rtf()
    buf = head.decode("latin-1")
    eof = False
    while not eof:
        block = f.read(BLOCK_BYTES)
        eof = not block
        buf += block.decode("latin-1")
        pos = 0
        for m in _TOKEN.finditer(buf):
            # a control token may continue in the next block; a text run can simply be split
            if not eof and m.end() >= len(buf) - 1 and m.group(6) is None:
                break
            rtf.feed_token(m)
            pos = m.end()
        buf = buf[pos:]
        if eof:
            rtf.flush_bytes()
        if text := rtf.take():
            yield text
