"""Turning user queries into FTS5 expressions and running them."""

from __future__ import annotations

import html
import os
import sqlite3
import unicodedata
from datetime import datetime, timedelta

import regex

from . import db, textnorm

HIT_LIMIT = 2000  # chunk hits considered per query; results are grouped per file
SNIPPET_TOKENS = 24
SNIPPET_CHARS = 80  # context on each side of a hit that the word index did not find (and so has no snippet())
MAX_REGEX_CHARS = 200
# The user's pattern runs on every path and on every chunk of stored text. The stdlib `re` holds the GIL while it
# matches, so one catastrophic pattern froze the whole service; `regex` can give the GIL back (concurrent=True)
# and stops a match by itself. A path is a few hundred characters and a chunk 16,000 (chunk_chars), so a match
# that needs this long is a runaway, not a slow pattern.
REGEX_PATH_TIMEOUT_S = 0.5
REGEX_CHUNK_TIMEOUT_S = 1.0

_TERM = regex.compile(r'(-?)"([^"]*)"(\*?)|(\S+)')
_WORDISH = regex.compile(r"\w")


def _phrase(text: str, *, prefix: bool) -> str | None:
    norm = textnorm.normalize(text).strip()
    if not _WORDISH.search(norm):
        return None
    if norm.startswith(textnorm.RUN_START) and len(norm) == 3:
        prefix = True  # a single CJK char: match bigrams starting with it
    return '"' + norm.replace('"', '""') + '"' + ("*" if prefix else "")


def _fts_term(text: str, *, prefix: bool) -> str:
    expr = _phrase(text, prefix=prefix)
    assert expr is not None, "_parse keeps only terms that contain a word character"
    return expr


def _parse(query: str) -> tuple[list[list[tuple[str, bool]]], list[str]]:
    """User syntax -> (OR-separated groups of (text, prefix) terms that must all match, excluded texts)."""
    groups: list[list[tuple[str, bool]]] = [[]]
    negatives: list[str] = []
    for m in _TERM.finditer(query):
        neg, phrase, star, word = m.groups()
        if word is not None:
            if word == "OR":
                if groups[-1]:
                    groups.append([])
                continue
            neg = "-" if word.startswith("-") and len(word) > 1 else ""
            word = word[1:] if neg else word
            word = word.lstrip("*?")  # a wildcard at the front says nothing: words are found inside words anyway
            star = "*" if word.endswith("*") else ""
            phrase = word.rstrip("*?")  # the same at the end; a final question mark is just punctuation
        phrase = phrase.strip("*?")
        if not _WORDISH.search(textnorm.normalize(phrase)):
            continue
        if neg:
            negatives.append(phrase)
        else:
            groups[-1].append((phrase, bool(star)))
    return [g for g in groups if g], negatives


def build_match(query: str) -> str | None:
    """User syntax: words, "phrases", prefix*, -exclude, OR. Everything else is literal."""
    return _to_match(*_parse(query))


def _is_wild(text: str) -> bool:
    """A `*` or `?` inside a term (leading and trailing ones are stripped by _parse): FTS5 cannot match those."""
    return "*" in text or "?" in text


def _to_match(groups: list[list[tuple[str, bool]]], negatives: list[str]) -> str | None:
    """The FTS5 expression for the part of the query the word index can answer; the scan answers the rest."""
    groups = [g for g in groups if not any(_is_wild(text) for text, _ in g)]
    negatives = [text for text in negatives if not _is_wild(text)]
    if not groups:
        return None
    expr = " OR ".join(" ".join(_fts_term(text, prefix=prefix) for text, prefix in g) for g in groups)
    if negatives:
        expr = f"({expr}) NOT ({' OR '.join(_fts_term(text, prefix=False) for text in negatives)})"
    return expr


def _name_key(text: str) -> str:
    """Case- and accent-insensitive form for comparing against folder and file names (as the FTS tokenizer does)."""
    if text.isascii():
        return text.lower()
    decomposed = unicodedata.normalize("NFD", textnorm.fold(text).casefold())
    return "".join(c for c in decomposed if unicodedata.category(c) != "Mn")


def _like_pattern(text: str) -> str:
    """LIKE pattern (ESCAPE '^') for a term: `*` is any text, `?` one character, the rest is literal."""
    escaped = text.replace("^", "^^").replace("%", "^%").replace("_", "^_")
    return escaped.replace("*", "%").replace("?", "_")


def _name_condition(groups: list[list[tuple[str, bool]]], negatives: list[str], *,
                    case_sensitive: bool = False) -> tuple[str, list[str]]:
    """SQL for "the path contains the words", built from the same parse as the content query.

    The words are compared without case and accents unless `case_sensitive` (then exactly as typed, with
    GLOB). `*` and `?` inside a word are wildcards.
    """
    path = "f.dir || '\\' || f.name"
    if case_sensitive:
        test = f"({path}) GLOB ?"

        def param(text: str) -> str:
            return "*" + text.replace("[", "[[]") + "*"
    else:
        test = f"sindex_key({path}) LIKE ? ESCAPE '^'"

        def param(text: str) -> str:
            return "%" + _like_pattern(_name_key(text)) + "%"

    params: list[str] = []
    ors = []
    for group in groups:
        ors.append("(" + " AND ".join(test for _ in group) + ")")
        params += [param(text) for text, _ in group]
    cond = "(" + " OR ".join(ors) + ")"
    for text in negatives:
        cond += f" AND NOT ({test})"
        params.append(param(text))
    return cond, params


def _user_regex(pattern: str, *, case_sensitive: bool, multiline: bool) -> regex.Pattern[str]:
    """The compiled pattern the user typed; ValueError says what is wrong with it."""
    if not pattern.strip():
        raise ValueError("Type a regular expression to search for.")
    if len(pattern) > MAX_REGEX_CHARS:
        raise ValueError(f"The regular expression is longer than {MAX_REGEX_CHARS} characters.")
    flags = (0 if case_sensitive else regex.IGNORECASE) | (regex.MULTILINE if multiline else 0)
    try:
        return regex.compile(pattern, flags)
    except regex.error as e:
        raise ValueError(f"Invalid regular expression: {e}") from e


def _term_pattern(text: str) -> str:
    """Regular expression for one query term: the letters as typed, `*` any word characters, `?` one."""
    out: list[str] = []
    in_space = False
    for ch in textnorm.fold(text):  # the stored text went through fold() too
        if ch.isspace():
            if not in_space:
                out.append(r"\s+")
            in_space = True
            continue
        in_space = False
        out.append(r"\w*" if ch == "*" else r"\w" if ch == "?" else regex.escape(ch))
    return "".join(out)


class _ChunkMatcher:
    """Does a chunk of stored text satisfy the query? The word index only knows whole words and folds case, so this
    is the "contains" rule behind words found inside words, case sensitivity, wildcards and regular expressions.

    It is called by SQLite for every chunk it scans. `timed_out` tells the caller that a pattern did not finish.
    """

    __slots__ = ("groups", "negatives", "timed_out")

    def __init__(self, groups: list[list[regex.Pattern[str]]], negatives: list[regex.Pattern[str]]) -> None:
        self.groups = groups  # OR of AND-lists; a chunk matches when all patterns of one list are found in it
        self.negatives = negatives
        self.timed_out = False

    @classmethod
    def for_terms(cls, groups: list[list[tuple[str, bool]]], negatives: list[str], *,
                  case_sensitive: bool) -> _ChunkMatcher:
        flags = 0 if case_sensitive else regex.IGNORECASE
        return cls([[regex.compile(_term_pattern(text), flags) for text, _ in group] for group in groups],
                   [regex.compile(_term_pattern(text), flags) for text in negatives])

    @staticmethod
    def _search(pattern: regex.Pattern[str], text: str) -> regex.Match[str] | None:
        return pattern.search(text, timeout=REGEX_CHUNK_TIMEOUT_S, concurrent=True)

    def __call__(self, body: str) -> bool:
        text = textnorm.decode_display(body)
        try:
            if not any(all(self._search(p, text) for p in group) for group in self.groups):
                return False
            return not any(self._search(p, text) for p in self.negatives)
        except TimeoutError:
            self.timed_out = True
            raise

    def lacks_negatives(self, body: str) -> bool:
        """True when none of the excluded terms is inside the chunk (words found inside words count)."""
        text = textnorm.decode_display(body)
        try:
            return not any(self._search(p, text) for p in self.negatives)
        except TimeoutError:
            self.timed_out = True
            raise

    def has_negative(self, body: str) -> bool:
        return not self.lacks_negatives(body)

    def snippet(self, body: str) -> str:
        """A piece of the chunk around the first hit, with the hits between highlight markers."""
        text = textnorm.decode_display(body)
        patterns = [p for group in self.groups for p in group]
        try:
            first = min((m for p in patterns if (m := self._search(p, text))), key=lambda m: m.start(), default=None)
            if first is None:
                return text[:2 * SNIPPET_CHARS]
            lo, hi = max(0, first.start() - SNIPPET_CHARS), min(len(text), first.end() + SNIPPET_CHARS)
            window = text[lo:hi]
            for p in patterns:
                window = p.sub(lambda m: textnorm.HL_START + m.group() + textnorm.HL_END if m.group() else "",
                               window, timeout=REGEX_CHUNK_TIMEOUT_S, concurrent=True)
        except TimeoutError:
            return text[:2 * SNIPPET_CHARS]  # the hit was found; only its highlighting was too slow
        return ("…" if lo else "") + window + ("…" if hi < len(text) else "")


def snippet_html(raw: str) -> str:
    text = " ".join(textnorm.decode_display(raw).split())
    text = html.escape(text)
    return text.replace(textnorm.HL_START, "<mark>").replace(textnorm.HL_END, "</mark>")


def _media_condition(*, kind: str | None, camera: str | None, has_gps: bool | None,
                      min_width: int | None, min_height: int | None,
                      min_duration_s: float | None, max_duration_s: float | None,
                      taken_after: str | None, taken_before: str | None) -> tuple[list[str], list]:
    """SQL fragments (against the `m` alias of `media`) and their params for the given media filters."""
    where: list[str] = []
    params: list = []
    if kind:
        where.append("m.kind = ?")
        params.append(kind)
    if camera:
        where.append("m.camera LIKE ? ESCAPE '^'")
        params.append("%" + camera.replace("^", "^^").replace("%", "^%").replace("_", "^_") + "%")
    if has_gps is not None:
        where.append("m.lat IS NOT NULL" if has_gps else "m.lat IS NULL")
    if min_width is not None:
        where.append("m.width >= ?")
        params.append(min_width)
    if min_height is not None:
        where.append("m.height >= ?")
        params.append(min_height)
    if min_duration_s is not None:
        where.append("m.duration_s >= ?")
        params.append(min_duration_s)
    if max_duration_s is not None:
        where.append("m.duration_s <= ?")
        params.append(max_duration_s)
    if taken_after:
        where.append("m.taken_at >= ?")
        params.append(taken_after)
    if taken_before:
        # taken_at is ISO text ("YYYY-MM-DDTHH:MM:SS"); a lexical range against the next day is correct
        hi = (datetime.strptime(taken_before, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        where.append("m.taken_at < ?")
        params.append(hi)
    return where, params


def _date_ns(value: str | None, *, end: bool = False) -> int | None:
    if not value:
        return None
    day = datetime.strptime(value, "%Y-%m-%d") + (timedelta(days=1) if end else timedelta())
    # local midnight as UTC epoch ns; plain arithmetic because Windows' timestamp()
    # rejects dates before 1970
    local_offset = datetime.now().astimezone().utcoffset() or timedelta()
    ns = (day - datetime(1970, 1, 1) - local_offset) // timedelta(microseconds=1) * 1000
    return max(-(2**63), min(ns, 2**63 - 1))  # SQLite integers are 64-bit


def search(conn: sqlite3.Connection, query: str, path: str | None = None, exts: list[str] | None = None,
           after: str | None = None, before: str | None = None,
           offset: int = 0, limit: int = 20, *, names: bool = False,
           regex: bool = False, case_sensitive: bool = False, contains: bool = True,
           kind: str | None = None, camera: str | None = None, has_gps: bool | None = None,
           min_width: int | None = None, min_height: int | None = None,
           min_duration_s: float | None = None, max_duration_s: float | None = None,
           taken_after: str | None = None, taken_before: str | None = None) -> dict:
    """Files whose text matches `query`; with `names`, also files whose folder or file name matches it.

    Hits are ranked in three steps: the word index first (whole words and prefixes, accents ignored), then a
    scan of the stored text for the same words *inside* other words (`contains`), then name matches, which have
    `hits == 0` and no snippet. The scan reads every chunk, about a second and a half per GB of index.
    `*` and `?` inside a word are wildcards.

    `names`, `regex` and `case_sensitive` are independent. `case_sensitive` compares the typed letters exactly,
    in contents and, with `names`, in names. `regex` makes the whole query one regular expression for the
    contents (and, with `names`, for the path: folder, backslash, file name). Case sensitivity and regular
    expressions cannot use the word index and always scan.

    `kind` and the other media filters narrow the results to images/videos with matching `media` fields; with
    an empty `query` and at least one media filter set, this browses by filter alone (no keyword required).
    """
    hit_limit = HIT_LIMIT  # read here so that a test can lower it
    groups, negatives = _parse(query)
    # only exclusions ("-kyo"): every file whose text has none of them, which needs the scan to be known
    negative_only = not regex and not groups and bool(negatives) and (contains or case_sensitive)
    matcher = rx_path = None
    if regex:
        try:
            matcher = _ChunkMatcher([[_user_regex(query, case_sensitive=case_sensitive, multiline=True)]], [])
            if names:
                rx_path = _user_regex(query, case_sensitive=case_sensitive, multiline=False)
        except ValueError as e:
            return {"results": [], "total": 0, "capped": False, "error": str(e)}
        has_query, match = True, None
    else:
        has_query = bool(groups) or negative_only
        match = _to_match(groups, negatives)
        if has_query:
            matcher = _ChunkMatcher.for_terms(groups, negatives, case_sensitive=case_sensitive)

    where = []
    params: list = []
    if path:
        root = os.path.normpath(path)
        where.append("(f.dir = ? OR f.dir LIKE ? ESCAPE '^')")
        params += [root, db.like_under(root)]
    if exts:
        where.append("(" + " OR ".join("f.name LIKE ? ESCAPE '^'" for _ in exts) + ")")
        params += ["%." + e.lower().lstrip(".").replace("^", "^^").replace("%", "^%").replace("_", "^_")
                   for e in exts]
    if (lo := _date_ns(after)) is not None:
        where.append("f.mtime_ns >= ?")
        params.append(lo)
    if (hi := _date_ns(before, end=True)) is not None:
        where.append("f.mtime_ns < ?")
        params.append(hi)

    media_where, media_params = _media_condition(
        kind=kind, camera=camera, has_gps=has_gps, min_width=min_width, min_height=min_height,
        min_duration_s=min_duration_s, max_duration_s=max_duration_s,
        taken_after=taken_after, taken_before=taken_before)
    media_join = "JOIN media m ON m.file_id = f.id" if media_where else ""
    chunk_join = f"FROM chunks JOIN files f ON f.id = (chunks.rowid >> {db.CHUNK_BITS}) {media_join}"

    best: dict[int, int | None] = {}  # file id -> best chunk rowid (None: name match only), in rank order
    hits: dict[int, int] = {}
    indexed: set[int] = set()  # the rowids in `best` that the word index found, so snippet() can show them
    capped = False

    if not has_query:
        if not media_where:
            return {"results": [], "total": 0, "capped": False, "error": "Type some words to search for."}
        # no keyword: browse by the media/path/ext/date filters alone, newest first
        sql = (f"SELECT f.id FROM files f {media_join} "
               f"WHERE {' AND '.join([*where, *media_where])} ORDER BY f.mtime_ns DESC LIMIT ?")
        fids = [r[0] for r in conn.execute(sql, [*params, *media_params, hit_limit])]
        best = dict.fromkeys(fids)
        capped = len(fids) >= hit_limit
    elif negative_only:
        assert matcher is not None, "a query has a matcher"
        conn.create_function("sindex_excluded", 1, matcher.has_negative, deterministic=True)
        sql = (f"SELECT DISTINCT chunks.rowid >> {db.CHUNK_BITS} {chunk_join} "
               f"WHERE {' AND '.join([*where, *media_where, 'sindex_excluded(chunks.body)'])}")
        try:
            excluded = {r[0] for r in conn.execute(sql, [*params, *media_params])}
        except sqlite3.OperationalError:
            if matcher.timed_out:
                return {"results": [], "total": 0, "capped": False, "error": "A pattern took too long on some text."}
            raise  # a cancelled or timed-out search, or a real error
        if not case_sensitive and (words := _to_match([[(text, False)] for text in negatives], [])) is not None:
            # the scan compares accents, the word index does not: "-cafe" must drop "café" as a plain search finds it
            sql = (f"SELECT DISTINCT chunks.rowid >> {db.CHUNK_BITS} {chunk_join} "
                   f"WHERE {' AND '.join(['chunks MATCH ?', *where, *media_where])}")
            excluded.update(r[0] for r in conn.execute(sql, [words, *params, *media_params]))
        if names:
            conn.create_function("sindex_key", 1, _name_key, deterministic=True)
            cond, name_params = _name_condition([[(text, False)] for text in negatives], [],
                                                case_sensitive=case_sensitive)
            sql = f"SELECT f.id FROM files f {media_join} WHERE {' AND '.join([cond, *where, *media_where])}"
            excluded.update(r[0] for r in conn.execute(sql, [*name_params, *params, *media_params]))
        # files without indexed text (binary, too big, failed) are not "without kyo", they are unknown
        sql = (f"SELECT f.id FROM files f {media_join} "
               f"WHERE {' AND '.join(['f.status IN (?, ?)', *where, *media_where])} ORDER BY f.mtime_ns DESC")
        fids = []
        for (fid,) in conn.execute(sql, ["ok", "truncated", *params, *media_params]):
            if fid in excluded:
                continue
            if len(fids) == hit_limit:
                capped = True
                break
            fids.append(fid)
        best = dict.fromkeys(fids)
    else:
        assert matcher is not None, "a query has a matcher"
        conn.create_function("sindex_chunk", 1, matcher, deterministic=True)
        conn.create_function("sindex_clean", 1, matcher.lacks_negatives, deterministic=True)
        seen: set[int] = set()

        def add(rowid: int, *, from_index: bool) -> None:
            if rowid in seen:
                return
            seen.add(rowid)
            fid = rowid >> db.CHUNK_BITS
            if fid not in best or best[fid] is None:
                best[fid] = rowid
                if from_index:
                    indexed.add(rowid)
            hits[fid] = hits.get(fid, 0) + 1

        def slow_pattern() -> dict:
            return {"results": [], "total": 0, "capped": False,
                    "error": f"The regular expression took longer than {REGEX_CHUNK_TIMEOUT_S:g} s on some text; "
                             "make it simpler or more specific."}

        if match is not None:
            # the word index cannot tell "kyo" from "tokyo", so what it found is checked against the exclusions
            # on the stored text; the same text keeps the letters as they were written for case sensitivity
            exact = (["sindex_chunk(chunks.body)"] if case_sensitive
                     else ["sindex_clean(chunks.body)"] if negatives else [])
            sql = (f"SELECT chunks.rowid {chunk_join} "
                   f"WHERE {' AND '.join(['chunks MATCH ?', *where, *media_where, *exact])} ORDER BY rank LIMIT ?")
            try:
                rowids = [r[0] for r in conn.execute(sql, [match, *params, *media_params, hit_limit])]
            except sqlite3.OperationalError as e:
                if matcher.timed_out:
                    return slow_pattern()
                return {"results": [], "total": 0, "capped": False, "error": f"Query not understood: {e}"}
            capped = len(rowids) >= hit_limit
            for rowid in rowids:
                add(rowid, from_index=True)

        if regex or case_sensitive or contains:
            sql = (f"SELECT chunks.rowid {chunk_join} "
                   f"WHERE {' AND '.join([*where, *media_where, 'sindex_chunk(chunks.body)'])} LIMIT ?")
            try:
                scanned = [r[0] for r in conn.execute(sql, [*params, *media_params, hit_limit])]
            except sqlite3.OperationalError:
                if matcher.timed_out:
                    return slow_pattern()
                raise  # a cancelled or timed-out search, or a real error
            capped = capped or len(scanned) >= hit_limit
            for rowid in scanned:
                add(rowid, from_index=False)

        if names:
            # the names are not in the FTS index, so this scans the files table; that is why it is opt-in
            if rx_path is not None:
                slow: list[str] = []  # the first path the pattern did not finish on

                def path_matches(text: str) -> bool:
                    try:
                        return rx_path.search(text, timeout=REGEX_PATH_TIMEOUT_S, concurrent=True) is not None
                    except TimeoutError:
                        slow.append(text)
                        raise

                conn.create_function("sindex_re", 1, path_matches, deterministic=True)
                cond, name_params = "sindex_re(f.dir || ? || f.name)", ["\\"]
            else:
                conn.create_function("sindex_key", 1, _name_key, deterministic=True)
                cond, name_params = _name_condition(groups, negatives, case_sensitive=case_sensitive)
            sql = (f"SELECT f.id FROM files f {media_join} "
                   f"WHERE {' AND '.join([cond, *where, *media_where])} LIMIT ?")
            try:
                by_name = [r[0] for r in conn.execute(sql, [*name_params, *params, *media_params, hit_limit])]
            except sqlite3.OperationalError:
                if not (rx_path is not None and slow):
                    raise
                return {"results": [], "total": 0, "capped": False,
                        "error": f"The regular expression took longer than {REGEX_PATH_TIMEOUT_S:g} s on {slow[0]}; "
                                 "make it simpler or more specific."}
            for fid in by_name:
                best.setdefault(fid, None)
            capped = capped or len(by_name) >= hit_limit

    if len(best) > hit_limit:
        best = dict(list(best.items())[:hit_limit])
        capped = True

    results = []
    for fid, rowid in list(best.items())[offset:offset + limit]:
        f = conn.execute(
            "SELECT f.dir, f.name, f.size, f.mtime_ns, f.status, m.kind, m.width, m.height, m.duration_s, "
            "m.taken_at, m.camera FROM files f LEFT JOIN media m ON m.file_id = f.id WHERE f.id = ?",
            (fid,)).fetchone()
        if f is None:
            continue
        snippet = ""
        if rowid is not None:
            if rowid in indexed and not case_sensitive:
                row = conn.execute(
                    f"SELECT snippet(chunks, 0, ?, ?, '…', {SNIPPET_TOKENS}) FROM chunks "
                    "WHERE chunks MATCH ? AND rowid = ?",
                    (textnorm.HL_START, textnorm.HL_END, match, rowid)).fetchone()
            else:
                row = conn.execute("SELECT body FROM chunks WHERE rowid = ?", (rowid,)).fetchone()
                row = (matcher.snippet(row[0]),) if row and matcher else None
            snippet = snippet_html(row[0]) if row else ""
        results.append({
            "path": os.path.join(f[0], f[1]), "dir": f[0], "name": f[1], "size": f[2],
            "mtime": f[3] / 1e9, "status": f[4], "hits": hits.get(fid, 0),
            "snippet": snippet,
            "media": {"kind": f[5], "width": f[6], "height": f[7], "duration_s": f[8], "taken_at": f[9],
                      "camera": f[10]} if f[5] else None,
        })
    return {"results": results, "total": len(best), "capped": capped, "error": None,
            "negative_only": negative_only}
