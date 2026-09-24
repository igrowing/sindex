"""Turning user queries into FTS5 expressions and running them."""

from __future__ import annotations

import html
import os
import re
import sqlite3
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


def build_match(query: str) -> str | None:
    """User syntax: words, "phrases", prefix*, -exclude, OR. Everything else is literal."""
    positives: list[str] = []
    negatives: list[str] = []
    for m in _TERM.finditer(query):
        neg, phrase, star, word = m.groups()
        if word is not None:
            if word == "OR":
                if positives and positives[-1] != "OR":
                    positives.append("OR")
                continue
            neg = "-" if word.startswith("-") and len(word) > 1 else ""
            word = word[1:] if neg else word
            star = "*" if word.endswith("*") and len(word) > 1 else ""
            phrase = word.rstrip("*")
        expr = _phrase(phrase, prefix=bool(star))
        if expr:
            (negatives if neg else positives).append(expr)
    while positives and positives[-1] == "OR":
        positives.pop()
    if not positives:
        return None
    expr = " ".join(positives)
    if negatives:
        expr = f"({expr}) NOT ({' OR '.join(negatives)})"
    return expr


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
           offset: int = 0, limit: int = 20) -> dict:
    match = build_match(query)
    if match is None:
        return {"results": [], "total": 0, "capped": False, "error": "Type some words to search for."}

    where = ["chunks MATCH ?"]
    params: list = [match]
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
           f"WHERE {' AND '.join(where)} ORDER BY rank LIMIT {HIT_LIMIT}")
    try:
        rowids = [r[0] for r in conn.execute(sql, params)]
    except sqlite3.OperationalError as e:
        return {"results": [], "total": 0, "capped": False, "error": f"Query not understood: {e}"}

    best: dict[int, int] = {}  # file id -> best chunk rowid, in rank order
    hits: dict[int, int] = {}
    for rowid in rowids:
        fid = rowid >> db.CHUNK_BITS
        best.setdefault(fid, rowid)
        hits[fid] = hits.get(fid, 0) + 1

    results = []
    for fid, rowid in list(best.items())[offset:offset + limit]:
        f = conn.execute("SELECT dir, name, size, mtime_ns, status FROM files WHERE id = ?", (fid,)).fetchone()
        if f is None:
            continue
        snip = conn.execute(
            f"SELECT snippet(chunks, 0, ?, ?, '…', {SNIPPET_TOKENS}) FROM chunks WHERE chunks MATCH ? AND rowid = ?",
            (textnorm.HL_START, textnorm.HL_END, match, rowid)).fetchone()
        results.append({
            "path": os.path.join(f[0], f[1]), "dir": f[0], "name": f[1], "size": f[2],
            "mtime": f[3] / 1e9, "status": f[4], "hits": hits[fid],
            "snippet": snippet_html(snip[0]) if snip else "",
        })
    return {"results": results, "total": len(best), "capped": len(rowids) >= HIT_LIMIT, "error": None}
