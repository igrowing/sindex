"""Turning user queries into FTS5 expressions and running them."""

from __future__ import annotations

import html
import itertools
import os
import sqlite3
import time
import unicodedata
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import regex

from . import db, textnorm

HIT_LIMIT = 2000  # chunk hits considered per query; results are grouped per file
PROGRESS_EVERY_S = 0.5  # how often a search that reads text hands what it found so far to its `progress` callback
SNIPPET_TOKENS = 24
# the choices of the "Sort by" menu; "relevance" is the order of the search itself
SORTS = ("relevance", "path_asc", "path_desc", "name_asc", "name_desc", "size_asc", "size_desc",
         "date_asc", "date_desc")
PROXIMITY_MATCHES = 500  # hits of one term that are compared when measuring how close the terms are
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


Term = tuple[str, bool]  # (text, prefix): a word matches the start of a word, a quoted phrase only itself


def _parse(query: str) -> tuple[list[list[Term]], list[Term]]:
    """User syntax -> (OR-separated groups of terms that must all match, excluded terms).

    A bare word is a prefix (`kyo` finds kyo and kyoto), so the word index answers it. A `*` or `?` at the start
    or inside a word stays in it and makes the term wild (`*kyo` finds tokyo): only a scan of the text answers that.
    """
    groups: list[list[Term]] = [[]]
    negatives: list[Term] = []
    for m in _TERM.finditer(query):
        neg, phrase, star, word = m.groups()
        prefix = bool(star)
        if word is not None:
            if word == "OR":
                if groups[-1]:
                    groups.append([])
                continue
            neg = "-" if word.startswith("-") and len(word) > 1 else ""
            word = word[1:] if neg else word
            prefix = True
            phrase = word.rstrip("*?")  # a star at the end says what a word means anyway; a "?" there is punctuation
        else:
            phrase = phrase.strip("*?")
        if not _WORDISH.search(textnorm.normalize(phrase)):
            continue
        (negatives if neg else groups[-1]).append((phrase, prefix))
    return [g for g in groups if g], negatives


def build_match(query: str) -> str | None:
    """User syntax: words (prefixes), "phrases", -exclude, OR. Everything else is literal."""
    return _to_match(*_parse(query))


def _is_wild(text: str) -> bool:
    """A `*` or `?` at the start or inside a term (trailing ones are stripped by _parse): FTS5 cannot match those."""
    return "*" in text or "?" in text


def has_wildcards(query: str) -> bool:
    """Whether a term of `query` needs the scan of all the stored text (see _parse)."""
    groups, negatives = _parse(query)
    return any(_is_wild(text) for text, _ in [*negatives, *(term for g in groups for term in g)])


def is_slow(query: str, *, regex: bool, case_sensitive: bool, names_only: bool = False) -> bool:
    """Whether the search reads stored text beyond what the word index returns, so it can take long: the page warns
    about these and shows their results while they come in. A names-only search reads no text, only the file list."""
    return not names_only and (regex or case_sensitive or has_wildcards(query))


def _to_match(groups: list[list[Term]], negatives: list[Term]) -> str | None:
    """The FTS5 expression for the part of the query the word index can answer; the scan answers the rest."""
    groups = [g for g in groups if not any(_is_wild(text) for text, _ in g)]
    negatives = [term for term in negatives if not _is_wild(term[0])]
    if not groups:
        return None
    expr = " OR ".join(" ".join(_fts_term(text, prefix=prefix) for text, prefix in g) for g in groups)
    if negatives:
        expr = f"({expr}) NOT ({' OR '.join(_fts_term(text, prefix=prefix) for text, prefix in negatives)})"
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


def _name_condition(groups: list[list[Term]], negatives: list[Term], *, name: str,
                    case_sensitive: bool = False) -> tuple[str, list[str]]:
    """SQL for "the path contains the words", built from the same parse as the content query. `name` is the column
    of the file name (f.name or n.name); the folder is `d.path`.

    The words are compared without case and accents unless `case_sensitive` (then exactly as typed, with
    GLOB). `*` and `?` inside a word are wildcards.
    """
    path = f"d.path || '\\' || {name}"
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
    for text, _ in negatives:
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


def _has_punctuation(text: str) -> bool:
    """Whether a term holds a character the word index throws away (anything but letters, digits, marks, wildcards).

    The index would find the bare words of "kyo)" or "c++", so what it returns has to be checked against the text.
    """
    return any(unicodedata.category(ch)[0] not in "LNM" and not ch.isspace() and ch not in "*?"
               for ch in textnorm.fold(text))


def _term_pattern(text: str) -> str:
    """Regular expression for one query term: the letters as typed, `*` any word characters, `?` one.

    Like the word index, it matches at the start of a word (`kyo` is in kyoto, not in tokyo; `*kyo` is in both).
    A term that starts with punctuation brings its own boundary, and CJK text has no spaces between its words, so
    neither is anchored.
    """
    folded = textnorm.fold(text)
    first = folded[:1]
    anchored = bool(first) and (first.isalnum() or first in "*?_") and not textnorm.has_cjk(first)
    out: list[str] = [r"(?<!\w)"] if anchored else []
    in_space = False
    for ch in folded:  # the stored text went through fold() too
        if ch.isspace():
            if not in_space:
                out.append(r"\s+")
            in_space = True
            continue
        in_space = False
        out.append(r"\w*" if ch == "*" else r"\w" if ch == "?" else regex.escape(ch))
    return "".join(out)


def _closest_together(positions: list[list[int]]) -> tuple[int, int]:
    """(distance, first position) of the closest choice of one position from every list: the smallest distance
    between the first and the last of the chosen positions."""
    merged = sorted((pos, i) for i, found in enumerate(positions) for pos in found)
    have = [0] * len(positions)
    covered = lo = 0
    best: tuple[int, int] | None = None
    for pos, i in merged:
        covered += have[i] == 0
        have[i] += 1
        while covered == len(positions):
            span = pos - merged[lo][0]
            best = (span, merged[lo][0]) if best is None or span < best[0] else best
            j = merged[lo][1]
            have[j] -= 1
            covered -= have[j] == 0
            lo += 1
    assert best is not None, "every list had a position"
    return best


@dataclass(frozen=True, slots=True)
class Listed:
    """A file of the `names` table: listed by its folder and name alone, without an id, text, size or date."""

    dir_id: int
    name: str


Key = int | Listed  # what a search found: the id of a file with text (or none), or a file listed by name


def _batches(items: list, size: int = 500) -> Iterator[tuple[list, str]]:
    """`items` in parts small enough for one `IN (...)`, each with its question marks."""
    for i in range(0, len(items), size):
        batch = items[i:i + size]
        yield batch, ", ".join("?" for _ in batch)


def _folders(conn: sqlite3.Connection, dir_ids: list[int]) -> dict[int, str]:
    folders: dict[int, str] = {}
    for batch, marks in _batches(dir_ids):
        folders.update(conn.execute(f"SELECT id, path FROM dirs WHERE id IN ({marks})", batch).fetchall())
    return folders


def _on_disk(path: str) -> tuple[int | None, int | None]:
    """(size, mtime_ns) of a file listed by name, which the index does not keep."""
    try:
        st = os.stat(path)
    except OSError:
        return None, None  # gone (or locked) since the last scan, which drops it; shown without size and date
    return st.st_size, st.st_mtime_ns


def _file_rows(conn: sqlite3.Connection, keys: list[Key], *,
               on_disk: bool) -> dict[Key, tuple[str, str, int | None, int | None]]:
    """key -> (folder, name, size, mtime_ns) for these files. A file listed by name gets its size and date from the
    disk when `on_disk` asks for them (None when they cannot be read), and None for both otherwise."""
    rows: dict[Key, tuple[str, str, int | None, int | None]] = {}
    ids = [k for k in keys if not isinstance(k, Listed)]
    for batch, marks in _batches(ids):
        for fid, folder, name, size, mtime in conn.execute(
                "SELECT f.id, d.path, f.name, f.size, f.mtime_ns FROM files f JOIN dirs d ON d.id = f.dir_id "
                f"WHERE f.id IN ({marks})", batch):
            rows[fid] = (folder, name, size, mtime)
    listed = [k for k in keys if isinstance(k, Listed)]
    folders = _folders(conn, sorted({k.dir_id for k in listed}))
    for k in listed:
        if (folder := folders.get(k.dir_id)) is not None:  # else dropped from the index since it was found
            size, mtime = _on_disk(os.path.join(folder, k.name)) if on_disk else (None, None)
            rows[k] = (folder, k.name, size, mtime)
    return rows


def extension_of(name: str) -> str:
    """The extension of a file name as the extension menus list it: after the last dot, lower case, "" for none."""
    return os.path.splitext(name)[1].lstrip(".").lower()


def _extensions(conn: sqlite3.Connection, found: Found) -> dict[Key, str]:
    if found.extensions is None:
        found.extensions = {key: extension_of(row[1])
                            for key, row in _file_rows(conn, list(found.best), on_disk=False).items()}
    return found.extensions


def _sorted_by_file(conn: sqlite3.Connection, best: dict[Key, int | None], sort: str) -> dict[Key, int | None]:
    """`best` (file -> chunk) in the order of the file's path, name, size or date; equal files keep their order.
    A file without a size or date (listed by name, and gone from the disk) comes last either way."""
    column, _, direction = sort.partition("_")
    rows = _file_rows(conn, list(best), on_disk=column in ("size", "date"))
    value: Callable[[tuple[str, str, int | None, int | None]], Any] = {
        "path": lambda r: (r[0].casefold(), r[1].casefold()), "name": lambda r: (r[1].casefold(), r[0].casefold()),
        "size": lambda r: r[2], "date": lambda r: r[3]}[column]
    known = [key for key in best if key in rows and value(rows[key]) is not None]
    unknown = [key for key in best if key in rows and value(rows[key]) is None]
    ordered = sorted(known, key=lambda key: value(rows[key]), reverse=direction == "desc") + unknown
    return {key: best[key] for key in ordered}


class _ChunkMatcher:
    """Does a chunk of stored text satisfy the query? The word index folds case, drops punctuation and cannot match
    a wildcard at the start or inside a word, so this checks the text for case sensitivity, punctuation, wildcards
    and regular expressions.

    It is called by SQLite for every chunk it scans. `timed_out` tells the caller that a pattern did not finish.
    """

    __slots__ = ("groups", "negatives", "timed_out")

    def __init__(self, groups: list[list[regex.Pattern[str]]], negatives: list[regex.Pattern[str]]) -> None:
        self.groups = groups  # OR of AND-lists; a chunk matches when all patterns of one list are found in it
        self.negatives = negatives
        self.timed_out = False

    @classmethod
    def for_terms(cls, groups: list[list[Term]], negatives: list[Term], *,
                  case_sensitive: bool, only_punctuated: bool = False) -> _ChunkMatcher:
        """`only_punctuated` leaves the other terms unchecked (they match anything): it is the second look at what
        the word index found, which already compared those without case and accents."""
        flags = 0 if case_sensitive else regex.IGNORECASE

        def pattern(text: str) -> regex.Pattern[str]:
            return regex.compile("" if only_punctuated and not _has_punctuation(text) else _term_pattern(text), flags)

        return cls([[pattern(text) for text, _ in group] for group in groups],
                   [regex.compile(_term_pattern(text), flags) for text, _ in negatives])

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

    def _closest(self, text: str) -> tuple[int, int] | None:
        """(distance, first position) of the closest words of one group in `text`, or None when no group has two
        words to compare. The distance is in characters between the first letters of the words."""
        closest = None
        try:
            for group in self.groups:
                if len(group) < 2:
                    continue
                starts = [[m.start() for m in itertools.islice(
                    p.finditer(text, timeout=REGEX_CHUNK_TIMEOUT_S, concurrent=True), PROXIMITY_MATCHES)]
                    for p in group]
                if all(starts):
                    found = _closest_together(starts)
                    closest = found if closest is None else min(closest, found)
        except TimeoutError:
            pass  # a slow pattern only costs the chunk its proximity, the hit itself was found already
        return closest

    def proximity(self, body: str) -> int | None:
        """How far apart the searched words are in the chunk (see _closest)."""
        found = self._closest(textnorm.decode_display(body))
        return None if found is None else found[0]

    def lacks_negatives(self, body: str) -> bool:
        """True when none of the excluded terms is in the chunk."""
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
            if (closest := self._closest(text)) is not None:
                # words that belong together: show them together, not the first of them wherever it is
                start, end = closest[1], closest[1] + closest[0] + SNIPPET_CHARS // 2
            elif (first := min((m for p in patterns if (m := self._search(p, text))),
                               key=lambda m: m.start(), default=None)) is not None:
                start, end = first.start(), first.end()
            else:
                return text[:2 * SNIPPET_CHARS]
            lo, hi = max(0, start - SNIPPET_CHARS), min(len(text), end + SNIPPET_CHARS)
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


def _place_condition(path: str | None, exts: list[str] | None, *, table: str) -> tuple[list[str], list]:
    """SQL conditions (against `table`, the alias of files or names) for the folder and the extensions."""
    where: list[str] = []
    params: list = []
    if path:
        root = os.path.normpath(path)
        # the folders are picked once, not compared again for every file in them
        where.append(f"{table}.dir_id IN (SELECT id FROM dirs WHERE path = ? OR path LIKE ? ESCAPE '^')")
        params += [root, db.like_under(root)]
    if exts:
        where.append("(" + " OR ".join(f"{table}.name LIKE ? ESCAPE '^'" for _ in exts) + ")")
        params += ["%." + e.lower().lstrip(".").replace("^", "^^").replace("%", "^%").replace("_", "^_")
                   for e in exts]
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


class SearchError(ValueError):
    """The query cannot be run (a bad pattern, nothing to search for); the message is for the user."""


@dataclass(slots=True)
class Found:
    """What a search found, before it is sorted and cut into pages. The web server keeps it for a while, so that
    another sort order or page does not search (read all the text) again."""

    best: dict[Key, int | None]  # file -> best chunk rowid (None: name match only), in search order
    hits: dict[int, int]  # file id -> number of chunks that matched
    indexed: set[int]  # the rowids in `best` that the word index found, so snippet() can show them
    seen: set[int]  # every chunk that matched
    capped: bool
    negative_only: bool
    literal: bool  # the word index's answer was checked against the text (punctuation in a word)
    case_sensitive: bool
    match: str | None  # the FTS5 expression of the index step
    matcher: _ChunkMatcher | None
    by_proximity: bool  # a group of two or more words: relevance can rank by how close they are
    # file id -> (distance, chunk rowid) of the chunk in which the searched words are closest to each other;
    # worked out when first asked for
    closest: dict[int, tuple[int, int]] | None = None
    extensions: dict[Key, str] | None = None  # file -> extension (lower case, no dot, "" for none); same

    @classmethod
    def empty(cls) -> Found:
        """Nothing found (yet): what a search that has just started shows."""
        return cls(best={}, hits={}, indexed=set(), seen=set(), capped=False, negative_only=False, literal=False,
                   case_sensitive=False, match=None, matcher=None, by_proximity=False)

    def snapshot(self) -> Found:
        """A copy that the search still going on does not change. It is not ranked by proximity: that reads the
        text of every hit again, and would be redone for every copy."""
        return Found(best=dict(self.best), hits=dict(self.hits), indexed=set(self.indexed), seen=set(self.seen),
                     capped=self.capped, negative_only=self.negative_only, literal=self.literal,
                     case_sensitive=self.case_sensitive, match=self.match, matcher=self.matcher, by_proximity=False)


def find(conn: sqlite3.Connection, query: str, path: str | None = None, exts: list[str] | None = None,
         after: str | None = None, before: str | None = None, *, names: bool = False, names_only: bool = False,
         regex: bool = False, case_sensitive: bool = False, scan: bool = True,
         kind: str | None = None, camera: str | None = None, has_gps: bool | None = None,
         min_width: int | None = None, min_height: int | None = None,
         min_duration_s: float | None = None, max_duration_s: float | None = None,
         taken_after: str | None = None, taken_before: str | None = None,
         progress: Callable[[Found], None] | None = None) -> Found:
    """The files whose text matches `query`; with `names`, also those whose folder or file name matches it.
    SearchError says why a query cannot be run.

    A word matches the start of a word (`kyo` finds kyo and kyoto, not tokyo; accents and case are ignored), which
    the word index answers at once. Hits are ranked in three steps: the word index, then a scan of the stored text
    for what the index cannot answer, then name matches, which have `hits == 0` and no snippet. Only `regex` and a
    `*` or `?` at the start or inside a word (`*kyo` finds tokyo, `t?kyo`, `ky*to`) need the scan, which reads
    every chunk: on a 4 GB index 35-65 s when its pages are not in the disk cache, about 8 s when they are.
    With `scan=False` the scan never runs and wild terms find nothing.

    `names_only` searches the folder and file names alone (it implies `names`): no stored text is read, so there are
    no snippets and no scan, however many wildcards, `regex` or `case_sensitive` ask for. The names searched include
    the files whose content is not read (index_all_names); the index keeps no date or media fields for those, so a
    date or media filter leaves them out.

    `names`, `regex` and `case_sensitive` are independent. `case_sensitive` compares the typed letters exactly,
    in contents and, with `names`, in names; in contents it checks the text of what the word index found, which
    takes as long as the word is common. `regex` makes the whole query one regular expression for the contents
    (and, with `names`, for the path: folder, backslash, file name).

    `progress`, when given, receives a snapshot of the files found so far about every PROGRESS_EVERY_S while text
    is checked or scanned, so that the page can show them before the search ends.

    `kind` and the other media filters narrow the results to images/videos with matching `media` fields; with
    an empty `query` and at least one media filter set, this browses by filter alone (no keyword required).
    At most HIT_LIMIT files are kept (`capped` says it was reached).
    """
    hit_limit = HIT_LIMIT  # read here so that a test can lower it
    names = names or names_only
    groups, negatives = _parse(query)
    wild_negatives = [term for term in negatives if _is_wild(term[0])]
    scanning = not names_only and (
        regex or (scan and (bool(wild_negatives) or any(_is_wild(text) for g in groups for text, _ in g))))
    # only exclusions ("-kyo"): every file whose text has none of them
    negative_only = not regex and not groups and bool(negatives)
    matcher = rx_path = None
    if regex:
        try:
            matcher = _ChunkMatcher([[_user_regex(query, case_sensitive=case_sensitive, multiline=True)]], [])
            if names:
                rx_path = _user_regex(query, case_sensitive=case_sensitive, multiline=False)
        except ValueError as e:
            raise SearchError(str(e)) from e
        has_query, match = True, None
    else:
        has_query = bool(groups) or negative_only
        # the index cannot see case: with case sensitivity the exclusions are checked on the text only
        match = None if names_only else _to_match(groups, [] if case_sensitive else negatives)
        if has_query:
            matcher = _ChunkMatcher.for_terms(groups, negatives, case_sensitive=case_sensitive)

    where, params = _place_condition(path, exts, table="f")
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
    named_files = f"FROM files f JOIN dirs d ON d.id = f.dir_id {media_join}"  # for a condition on the path
    # the files listed by name alone: no date or media fields to filter them by
    listed_too = names and not media_where and after is None and before is None
    listed_where, listed_params = _place_condition(path, exts, table="n")

    def listed(cond: str, cond_params: list, *, limit: int) -> list[Key]:
        sql = (f"SELECT n.dir_id, n.name FROM names n JOIN dirs d ON d.id = n.dir_id "
               f"WHERE {' AND '.join([cond, *listed_where])} LIMIT ?")
        return [Listed(dir_id, name) for dir_id, name in conn.execute(sql, [*cond_params, *listed_params, limit])]

    # what the index found for a term with punctuation is only a guess: "kyo)" is the word kyo to it
    literal = not regex and not names_only and any(_has_punctuation(text) for g in groups for text, _ in g)
    found = Found(best={}, hits={}, indexed=set(), seen=set(), capped=False, negative_only=negative_only,
                  literal=literal, case_sensitive=case_sensitive, match=match, matcher=matcher, by_proximity=False)
    next_report = time.monotonic() + PROGRESS_EVERY_S

    def report() -> None:
        nonlocal next_report
        if progress is not None and time.monotonic() >= next_report:
            progress(found.snapshot())
            next_report = time.monotonic() + PROGRESS_EVERY_S

    if not has_query:
        if not media_where:
            raise SearchError("Type some words to search for.")
        # no keyword: browse by the media/path/ext/date filters alone, newest first
        sql = (f"SELECT f.id FROM files f {media_join} "
               f"WHERE {' AND '.join([*where, *media_where])} ORDER BY f.mtime_ns DESC LIMIT ?")
        fids = [r[0] for r in conn.execute(sql, [*params, *media_params, hit_limit])]
        found.best = dict.fromkeys(fids)
        found.capped = len(fids) >= hit_limit
    elif negative_only:
        assert matcher is not None, "a query has a matcher"
        conn.create_function("sindex_excluded", 1, matcher.has_negative, deterministic=True)
        excluded: set[int] = set()
        if names_only:
            pass  # the text is not searched, so no text excludes a file
        elif wild_negatives:
            if not scan:
                raise SearchError("An excluded word with a * or ? at its start or inside needs the scan of all text.")
            sql = (f"SELECT DISTINCT chunks.rowid >> {db.CHUNK_BITS} {chunk_join} "
                   f"WHERE {' AND '.join([*where, *media_where, 'sindex_excluded(chunks.body)'])}")
            try:
                excluded = {r[0] for r in conn.execute(sql, [*params, *media_params])}
            except sqlite3.OperationalError:
                if matcher.timed_out:
                    raise SearchError("A pattern took too long on some text.") from None
                raise  # a cancelled or timed-out search, or a real error
        if not names_only and (words := _to_match([[term] for term in negatives], [])) is not None:
            # the word index finds the other excluded words; with case sensitivity their letters are checked too
            check = ["sindex_excluded(chunks.body)"] if case_sensitive else []
            sql = (f"SELECT DISTINCT chunks.rowid >> {db.CHUNK_BITS} {chunk_join} "
                   f"WHERE {' AND '.join(['chunks MATCH ?', *where, *media_where, *check])}")
            excluded.update(r[0] for r in conn.execute(sql, [words, *params, *media_params]))
        if names:
            conn.create_function("sindex_key", 1, _name_key, deterministic=True)
            cond, name_params = _name_condition([[term] for term in negatives], [], name="f.name",
                                                case_sensitive=case_sensitive)
            sql = f"SELECT f.id {named_files} WHERE {' AND '.join([cond, *where, *media_where])}"
            excluded.update(r[0] for r in conn.execute(sql, [*name_params, *params, *media_params]))
        # files without indexed text (binary, too big, failed) are not "without kyo", they are unknown; but their
        # names are known
        status = [] if names_only else ["f.status IN (?, ?)"]
        sql = (f"SELECT f.id FROM files f {media_join} "
               f"WHERE {' AND '.join([*status, *where, *media_where]) or '1'} ORDER BY f.mtime_ns DESC")
        keys: list[Key] = []
        for (fid,) in conn.execute(sql, [*([] if names_only else ["ok", "truncated"]), *params, *media_params]):
            if fid in excluded:
                continue
            if len(keys) == hit_limit:
                found.capped = True
                break
            keys.append(fid)
        if names_only and listed_too and not found.capped:
            cond, name_params = _name_condition([[term] for term in negatives], [], name="n.name",
                                                case_sensitive=case_sensitive)
            more = listed(f"NOT {cond}", name_params, limit=hit_limit - len(keys) + 1)
            found.capped = len(keys) + len(more) > hit_limit
            keys += more[:hit_limit - len(keys)]
        found.best = dict.fromkeys(keys)
    else:
        assert matcher is not None, "a query has a matcher"
        conn.create_function("sindex_chunk", 1, matcher, deterministic=True)
        conn.create_function("sindex_clean", 1, matcher.lacks_negatives, deterministic=True)
        if literal:
            conn.create_function("sindex_literal", 1, _ChunkMatcher.for_terms(
                groups, negatives, case_sensitive=case_sensitive, only_punctuated=True), deterministic=True)

        def add(rowid: int, *, from_index: bool) -> None:
            if rowid in found.seen:
                return
            found.seen.add(rowid)
            fid = rowid >> db.CHUNK_BITS
            if fid not in found.best or found.best[fid] is None:
                found.best[fid] = rowid
                if from_index:
                    found.indexed.add(rowid)
            found.hits[fid] = found.hits.get(fid, 0) + 1

        def slow_pattern() -> SearchError:
            return SearchError(f"The regular expression took longer than {REGEX_CHUNK_TIMEOUT_S:g} s on some text; "
                               "make it simpler or more specific.")

        if match is not None:  # (never with names_only)
            # what the index found is checked against the stored text, which keeps the letters as they were
            # written, when case matters, a word has punctuation, or an exclusion has a wildcard
            exact = (["sindex_chunk(chunks.body)"] if case_sensitive
                     else ["sindex_literal(chunks.body)"] if literal
                     else ["sindex_clean(chunks.body)"] if wild_negatives else [])
            cond = " AND ".join(["chunks MATCH ?", *where, *media_where, *exact])
            args = [match, *params, *media_params, hit_limit]
            try:
                if exact and progress is not None:
                    # checking the text of every hit takes as long as the word is common, and ORDER BY rank would
                    # check all of them before the first one comes out: hand them out as they come, rank at the end
                    ranked = []
                    for rowid, rank in conn.execute(f"SELECT chunks.rowid, rank {chunk_join} WHERE {cond} LIMIT ?",
                                                    args):
                        ranked.append((rank, rowid))
                        add(rowid, from_index=True)
                        report()
                    for part in (found.best, found.hits, found.indexed, found.seen):
                        part.clear()
                    rowids = [rowid for _, rowid in sorted(ranked)]
                else:
                    rowids = [r[0] for r in conn.execute(
                        f"SELECT chunks.rowid {chunk_join} WHERE {cond} ORDER BY rank LIMIT ?", args)]
            except sqlite3.OperationalError as e:
                if matcher.timed_out:
                    raise slow_pattern() from e
                raise SearchError(f"Query not understood: {e}") from e
            found.capped = len(rowids) >= hit_limit
            for rowid in rowids:
                add(rowid, from_index=True)

        if scanning:
            sql = (f"SELECT chunks.rowid {chunk_join} "
                   f"WHERE {' AND '.join([*where, *media_where, 'sindex_chunk(chunks.body)'])} LIMIT ?")
            scanned = 0
            try:
                for (rowid,) in conn.execute(sql, [*params, *media_params, hit_limit]):
                    scanned += 1
                    add(rowid, from_index=False)
                    report()
            except sqlite3.OperationalError:
                if matcher.timed_out:
                    raise slow_pattern() from None
                raise  # a cancelled or timed-out search, or a real error
            found.capped = found.capped or scanned >= hit_limit

        if names:
            # the names are not in the FTS index, so this scans the files and names tables; that is why it is opt-in
            slow: list[str] = []  # the first path the pattern did not finish on
            if rx_path is not None:

                def path_matches(text: str) -> bool:
                    try:
                        return rx_path.search(text, timeout=REGEX_PATH_TIMEOUT_S, concurrent=True) is not None
                    except TimeoutError:
                        slow.append(text)
                        raise

                conn.create_function("sindex_re", 1, path_matches, deterministic=True)

                def name_condition(name: str) -> tuple[str, list[str]]:
                    return f"sindex_re(d.path || ? || {name})", ["\\"]
            else:
                conn.create_function("sindex_key", 1, _name_key, deterministic=True)

                def name_condition(name: str) -> tuple[str, list[str]]:
                    return _name_condition(groups, negatives, name=name, case_sensitive=case_sensitive)
            cond, name_params = name_condition("f.name")
            sql = f"SELECT f.id {named_files} WHERE {' AND '.join([cond, *where, *media_where])} LIMIT ?"
            try:
                by_name: list[Key] = [r[0] for r in conn.execute(
                    sql, [*name_params, *params, *media_params, hit_limit])]
                if listed_too:
                    by_name += listed(*name_condition("n.name"), limit=hit_limit)
            except sqlite3.OperationalError:
                if not slow:
                    raise
                raise SearchError(f"The regular expression took longer than {REGEX_PATH_TIMEOUT_S:g} s on "
                                  f"{slow[0]}; make it simpler or more specific.") from None
            for key in by_name:
                found.best.setdefault(key, None)
            found.capped = found.capped or len(by_name) >= hit_limit

    if len(found.best) > hit_limit:
        found.best = dict(list(found.best.items())[:hit_limit])
        found.capped = True
    found.by_proximity = (matcher is not None and not regex and not names_only
                          and any(len(group) >= 2 for group in groups))
    return found


def _closest_chunks(conn: sqlite3.Connection, found: Found) -> dict[Key, tuple[int, int]]:
    """For each file with two or more searched words: how close they come in its best chunk, and which chunk that
    is. A file has many chunks; the one the word index ranks first is not necessarily the one with the words
    together, and the one with the words together is the one to rank the file by and to show."""
    if found.closest is None:
        assert found.matcher is not None, "proximity needs the words of the query"
        closest: dict[Key, tuple[int, int]] = {}
        for rowid in found.seen:
            fid = rowid >> db.CHUNK_BITS
            if fid not in found.best:
                continue
            row = conn.execute("SELECT body FROM chunks WHERE rowid = ?", (rowid,)).fetchone()
            if row and (span := found.matcher.proximity(row[0])) is not None:
                if fid not in closest or (span, rowid) < closest[fid]:  # the earlier chunk wins a tie
                    closest[fid] = (span, rowid)
        found.closest = closest
    return found.closest


def present(conn: sqlite3.Connection, found: Found, *, sort: str = "relevance", offset: int = 0,
            limit: int | None = 20, skip: frozenset[str] = frozenset()) -> dict:
    """One page of `found`, in the order of `sort` (one of SORTS), with a snippet for each file.
    `limit=None` is every file from `offset` on (the CSV export).

    The order is applied to all the files that were found, before the page is cut from it. "relevance" keeps
    the order of the search and, when the query has two or more words, puts the files where these words are
    closest to each other first. It costs little (no text is searched), so another sort or page of the same
    `found` is quick.
    """
    if sort not in SORTS:
        raise SearchError(f"Unknown sort order {sort!r}.")
    best = found.best
    extensions = _extensions(conn, found)
    closest = _closest_chunks(conn, found) if found.by_proximity else {}
    if closest:
        best = {fid: closest[fid][1] if fid in closest else rowid for fid, rowid in best.items()}  # show that chunk
    if sort != "relevance":
        best = _sorted_by_file(conn, best, sort)
    elif closest:
        # the files whose words are closer together first; files with nothing to compare keep the search order
        position = {fid: n for n, fid in enumerate(best)}
        ranked = sorted(best, key=lambda f: (f not in closest, closest[f][0] if f in closest else 0, position[f]))
        best = {fid: best[fid] for fid in ranked}
    counts: dict[str, int] = {}
    for ext in extensions.values():
        counts[ext] = counts.get(ext, 0) + 1
    # what is on offer to select: all found files, whatever is selected now
    offered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    if skip:
        best = {fid: rowid for fid, rowid in best.items() if extensions.get(fid) not in skip}
    matcher = found.matcher

    results = []
    page = list(best.items())[offset:None if limit is None else offset + limit]
    listed = _file_rows(conn, [key for key, _ in page if isinstance(key, Listed)], on_disk=True)
    for fid, rowid in page:
        if isinstance(fid, Listed):
            if (row := listed.get(fid)) is not None:
                folder, name, size, mtime = row
                results.append({"path": os.path.join(folder, name), "dir": folder, "name": name, "size": size,
                                "mtime": None if mtime is None else mtime / 1e9, "status": db.NAME_ONLY,
                                "hits": 0, "snippet": "", "media": None})
            continue
        f = conn.execute(
            "SELECT d.path, f.name, f.size, f.mtime_ns, f.status, m.kind, m.width, m.height, m.duration_s, "
            "m.taken_at, m.camera FROM files f JOIN dirs d ON d.id = f.dir_id "
            "LEFT JOIN media m ON m.file_id = f.id WHERE f.id = ?",
            (fid,)).fetchone()
        if f is None:
            continue
        snippet = ""
        if rowid is not None:
            if rowid in found.indexed and not found.case_sensitive and not found.literal and fid not in closest:
                row = conn.execute(
                    f"SELECT snippet(chunks, 0, ?, ?, '…', {SNIPPET_TOKENS}) FROM chunks "
                    "WHERE chunks MATCH ? AND rowid = ?",
                    (textnorm.HL_START, textnorm.HL_END, found.match, rowid)).fetchone()
            else:
                row = conn.execute("SELECT body FROM chunks WHERE rowid = ?", (rowid,)).fetchone()
                row = (matcher.snippet(row[0]),) if row and matcher else None
            snippet = snippet_html(row[0]) if row else ""
        results.append({
            "path": os.path.join(f[0], f[1]), "dir": f[0], "name": f[1], "size": f[2],
            "mtime": f[3] / 1e9, "status": f[4], "hits": found.hits.get(fid, 0),
            "snippet": snippet,
            "media": {"kind": f[5], "width": f[6], "height": f[7], "duration_s": f[8], "taken_at": f[9],
                      "camera": f[10]} if f[5] else None,
        })
    return {"results": results, "total": len(best), "capped": found.capped, "error": None,
            "negative_only": found.negative_only, "found_total": len(found.best),
            "extensions": [list(item) for item in offered]}


def search(conn: sqlite3.Connection, query: str, path: str | None = None, exts: list[str] | None = None,
           after: str | None = None, before: str | None = None, offset: int = 0, limit: int = 20, *,
           sort: str = "relevance", skip: frozenset[str] = frozenset(),
           **options: Any) -> dict:
    """find() and present() in one call, with a failure reported as {"error": ...}; `options` are those of find()."""
    try:
        return present(conn, find(conn, query, path, exts, after, before, **options),
                       sort=sort, offset=offset, limit=limit, skip=skip)
    except SearchError as e:
        return {"results": [], "total": 0, "capped": False, "error": str(e)}
