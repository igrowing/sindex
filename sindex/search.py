"""Turning user queries into FTS5 expressions and running them."""

from __future__ import annotations

import html
import os
import re
import sqlite3
import unicodedata
from datetime import datetime, timedelta

from . import db, textnorm

HIT_LIMIT = 2000  # chunk hits considered per query; results are grouped per file
SNIPPET_TOKENS = 24

_TERM = re.compile(r'(-?)"([^"]*)"(\*?)|(\S+)')
_WORDISH = re.compile(r"\w")


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
            star = "*" if word.endswith("*") and len(word) > 1 else ""
            phrase = word.rstrip("*")
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


def _to_match(groups: list[list[tuple[str, bool]]], negatives: list[str]) -> str | None:
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


def _name_condition(groups: list[list[tuple[str, bool]]], negatives: list[str]) -> tuple[str, list[str]]:
    """SQL for "the path contains the words", built from the same parse as the content query."""
    path = "sindex_key(f.dir || '\\' || f.name)"
    params: list[str] = []
    ors = []
    for group in groups:
        ors.append("(" + " AND ".join(f"instr({path}, ?) > 0" for _ in group) + ")")
        params += [_name_key(text) for text, _ in group]
    cond = "(" + " OR ".join(ors) + ")"
    for text in negatives:
        cond += f" AND instr({path}, ?) = 0"
        params.append(_name_key(text))
    return cond, params


def snippet_html(raw: str) -> str:
    text = " ".join(textnorm.decode_display(raw).split())
    text = html.escape(text)
    return text.replace(textnorm.HL_START, "<mark>").replace(textnorm.HL_END, "</mark>")


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
           offset: int = 0, limit: int = 20, *, names: bool = False) -> dict:
    """Files whose text matches `query`; with `names`, also files whose folder or file name contains the words.

    Name-only matches come after the content matches and have `hits == 0` and no snippet.
    """
    groups, negatives = _parse(query)
    match = _to_match(groups, negatives)
    if match is None:
        return {"results": [], "total": 0, "capped": False, "error": "Type some words to search for."}

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

    sql = (f"SELECT chunks.rowid FROM chunks JOIN files f ON f.id = (chunks.rowid >> {db.CHUNK_BITS}) "
           f"WHERE {' AND '.join(['chunks MATCH ?', *where])} ORDER BY rank LIMIT {HIT_LIMIT}")
    try:
        rowids = [r[0] for r in conn.execute(sql, [match, *params])]
    except sqlite3.OperationalError as e:
        return {"results": [], "total": 0, "capped": False, "error": f"Query not understood: {e}"}

    best: dict[int, int | None] = {}  # file id -> best chunk rowid (None: name match only), in rank order
    hits: dict[int, int] = {}
    for rowid in rowids:
        fid = rowid >> db.CHUNK_BITS
        best.setdefault(fid, rowid)
        hits[fid] = hits.get(fid, 0) + 1
    capped = len(rowids) >= HIT_LIMIT

    if names:
        # the names are not in the FTS index, so this scans the files table; that is why it is opt-in
        conn.create_function("sindex_key", 1, _name_key, deterministic=True)
        cond, name_params = _name_condition(groups, negatives)
        sql = f"SELECT f.id FROM files f WHERE {' AND '.join([cond, *where])} LIMIT {HIT_LIMIT}"
        by_name = [r[0] for r in conn.execute(sql, [*name_params, *params])]
        for fid in by_name:
            best.setdefault(fid, None)
        capped = capped or len(by_name) >= HIT_LIMIT

    results = []
    for fid, rowid in list(best.items())[offset:offset + limit]:
        f = conn.execute("SELECT dir, name, size, mtime_ns, status FROM files WHERE id = ?", (fid,)).fetchone()
        if f is None:
            continue
        snip = None
        if rowid is not None:
            snip = conn.execute(
                f"SELECT snippet(chunks, 0, ?, ?, '…', {SNIPPET_TOKENS}) FROM chunks "
                "WHERE chunks MATCH ? AND rowid = ?",
                (textnorm.HL_START, textnorm.HL_END, match, rowid)).fetchone()
        results.append({
            "path": os.path.join(f[0], f[1]), "dir": f[0], "name": f[1], "size": f[2],
            "mtime": f[3] / 1e9, "status": f[4], "hits": hits.get(fid, 0),
            "snippet": snippet_html(snip[0]) if snip else "",
        })
    return {"results": results, "total": len(best), "capped": capped, "error": None}
