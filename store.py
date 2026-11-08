"""SQLite storage for the recall index: files, chunks, an FTS5 mirror and a key/value meta table.

One database per Hermes profile, written under the plugin's data directory. No network, no
embeddings service — ranking is BM25 from SQLite's own FTS5, with a small deterministic boost
for path matches and recency so that "the invoice from March" beats an old buried chunk.
"""

from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS files (
    id      INTEGER PRIMARY KEY,
    path    TEXT UNIQUE NOT NULL,
    mtime   REAL NOT NULL DEFAULT 0,
    size    INTEGER NOT NULL DEFAULT 0,
    ext     TEXT NOT NULL DEFAULT '',
    chunks  INTEGER NOT NULL DEFAULT 0,
    skipped TEXT
);
CREATE TABLE IF NOT EXISTS chunks (
    id      INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL,
    ord     INTEGER NOT NULL,
    text    TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text,
    path UNINDEXED,
    file_id UNINDEXED,
    ord UNINDEXED,
    tokenize = 'unicode61 remove_diacritics 2'
);
CREATE INDEX IF NOT EXISTS chunks_file ON chunks(file_id);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # The plugin keeps one handle on the Runtime and serves it to whichever thread runs the
    # current tool call or ambient hook, so the connection must not be thread-bound. SQLite's
    # own locking plus the Runtime lock serialise actual use.
    conn = sqlite3.connect(str(db_path), timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    return conn


def set_meta(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def get_meta(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def file_state(conn: sqlite3.Connection, path: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM files WHERE path=?", (path,)).fetchone()


def drop_file(conn: sqlite3.Connection, path: str) -> None:
    row = conn.execute("SELECT id FROM files WHERE path=?", (path,)).fetchone()
    if not row:
        return
    fid = row["id"]
    conn.execute("DELETE FROM chunks_fts WHERE file_id=?", (fid,))
    conn.execute("DELETE FROM chunks WHERE file_id=?", (fid,))
    conn.execute("DELETE FROM files WHERE id=?", (fid,))


def put_file(
    conn: sqlite3.Connection,
    path: str,
    mtime: float,
    size: int,
    ext: str,
    chunks: list[str],
    skipped: str = "",
) -> None:
    drop_file(conn, path)
    cur = conn.execute(
        "INSERT INTO files(path, mtime, size, ext, chunks, skipped) VALUES(?,?,?,?,?,?)",
        (path, mtime, size, ext, len(chunks), skipped),
    )
    fid = cur.lastrowid
    # Batch the two inserts: a first run over a few thousand files writes ~100k chunk rows, and
    # per-row execution there is most of the indexing wall clock.
    conn.executemany(
        "INSERT INTO chunks(file_id, ord, text) VALUES(?,?,?)",
        [(fid, i, text) for i, text in enumerate(chunks)],
    )
    conn.executemany(
        "INSERT INTO chunks_fts(text, path, file_id, ord) VALUES(?,?,?,?)",
        [(text, path, fid, i) for i, text in enumerate(chunks)],
    )


_TOKEN = re.compile(r"[0-9A-Za-z_]{2,}")


def _is_phrase_hit(text: str, tokens: list[str]) -> bool:
    """True when every query token appears in the snippet and in query order (a phrase-ish match).

    Cheap on purpose: it runs on the snippet FTS5 already produced, so there is no second index and
    no extra query. It only nudges ranking — a document that says "powder coating price" in one
    sentence should beat a file that mentions the three words in three unrelated places.
    """
    flat = re.sub(r"[\[\]]", "", text or "").lower()
    pos = 0
    for tok in tokens:
        found = flat.find(tok, pos)
        if found < 0:
            return False
        pos = found + len(tok)
    return True


def query_tokens(query: str, limit: int = 12) -> list[str]:
    seen: list[str] = []
    for tok in _TOKEN.findall(query or ""):
        low = tok.lower()
        if low not in seen:
            seen.append(low)
        if len(seen) >= limit:
            break
    return seen


def build_match(query: str) -> str:
    """Turn free text into a forgiving FTS5 MATCH expression (term OR prefix per token)."""
    tokens = query_tokens(query)
    if not tokens:
        return ""
    parts = []
    for tok in tokens:
        safe = tok.replace('"', "")
        parts.append(f'("{safe}" OR {safe}*)')
    return " OR ".join(parts)


def search(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 8,
    path_filter: str = "",
    ext: str = "",
) -> list[dict[str, Any]]:
    match = build_match(query)
    if not match:
        return []
    # The extension and path filters belong in the query, not in a loop over an already-truncated
    # candidate window: filtering after `LIMIT 40` answers "no match" when the best hits for the
    # requested extension sit below the cut.
    wanted_ext = ext.lower().lstrip(".")
    where = ["chunks_fts MATCH ?"]
    params: list[Any] = [match]
    if wanted_ext:
        where.append("path LIKE ?")
        params.append("%." + wanted_ext)
    if path_filter:
        where.append("path LIKE ?")
        params.append("%" + path_filter + "%")
    params.append(max(limit * 12, 40))
    rows = conn.execute(
        f"""
        SELECT path,
               ord,
               snippet(chunks_fts, 0, '[', ']', ' … ', 18) AS snip,
               bm25(chunks_fts) AS bm25
        FROM chunks_fts
        WHERE {' AND '.join(where)}
        ORDER BY bm25
        LIMIT ?
        """,
        params,
    ).fetchall()
    if not rows:
        return []

    tokens = query_tokens(query)
    wanted_ext = ext.lower().lstrip(".")
    now = time.time()
    best: dict[str, dict[str, Any]] = {}
    for row in rows:
        path = row["path"]
        low = path.lower()
        # Re-checked exactly: the LIKE above is a coarse prefilter (`_` and `%` are wildcards there).
        if path_filter and path_filter.lower() not in low:
            continue
        if wanted_ext and not low.endswith("." + wanted_ext):
            continue
        # FTS5 bm25 is negative, better matches are more negative: normalise to a positive score.
        score = -float(row["bm25"])
        hits = sum(1 for t in tokens if t in low)
        score += 2.5 * hits
        name = Path(path).name.lower()
        if any(t in name for t in tokens):
            score += 3.0
        try:
            age_days = max(0.0, (now - Path(path).stat().st_mtime) / 86400.0)
        except OSError:
            age_days = 3650.0
        # Gentle recency favour: a year-old file keeps ~70% of the boost of a fresh one.
        score *= 1.0 + 0.35 / (1.0 + age_days / 90.0)
        snip = (row["snip"] or "").strip()
        if len(tokens) >= 2 and _is_phrase_hit(snip, tokens):
            score += 3.0
        is_name_chunk = row["ord"] == 0
        prev = best.get(path)
        if prev is None:
            best[path] = {
                "path": path,
                "name": Path(path).name,
                "score": round(score, 2),
                "snippet": snip,
                "chunk": row["ord"],
                "_name_chunk": is_name_chunk,
            }
            continue
        # Rank by the best chunk, but prefer a real (non-filename) chunk for the preview so a hit
        # never shows only the synthetic name row when its body also matched.
        if score > prev["score"]:
            prev["score"] = round(score, 2)
        if prev["_name_chunk"] and not is_name_chunk and snip:
            prev["snippet"], prev["chunk"], prev["_name_chunk"] = snip, row["ord"], False
    for item in best.values():
        item.pop("_name_chunk", None)

    out = sorted(best.values(), key=lambda d: d["score"], reverse=True)[:limit]
    for item in out:
        try:
            item["modified"] = time.strftime(
                "%Y-%m-%d %H:%M", time.localtime(Path(item["path"]).stat().st_mtime)
            )
        except OSError:
            item["modified"] = ""
    return out


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    files = conn.execute("SELECT COUNT(*) AS n FROM files").fetchone()["n"]
    chunks = conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
    skipped = conn.execute("SELECT COUNT(*) AS n FROM files WHERE skipped != ''").fetchone()["n"]
    return {
        "files": files,
        "chunks": chunks,
        "skipped_files": skipped,
        "last_index": get_meta(conn, "last_index", ""),
        "roots": get_meta(conn, "roots", ""),
        "db": get_meta(conn, "db_path", ""),
        "index_seconds": get_meta(conn, "index_seconds", ""),
    }


def forget_path(conn: sqlite3.Connection, target: str) -> int:
    """Drop one file, or everything under a folder prefix. Returns how many files were removed."""
    target = target.rstrip("/")
    rows = conn.execute(
        "SELECT path FROM files WHERE path = ? OR path LIKE ?",
        (target, target + "/%"),
    ).fetchall()
    for row in rows:
        drop_file(conn, row["path"])
    conn.commit()
    return len(rows)


def wipe(conn: sqlite3.Connection) -> None:
    """Empty the index but keep the schema and settings."""
    conn.execute("DELETE FROM chunks_fts")
    conn.execute("DELETE FROM chunks")
    conn.execute("DELETE FROM files")
    conn.execute(
        "DELETE FROM meta WHERE key IN ('last_index', 'roots', 'roots_json', 'index_seconds')"
    )
    conn.commit()


def iter_paths(conn: sqlite3.Connection, prefix: str = "") -> Iterable[str]:
    if prefix:
        rows = conn.execute(
            "SELECT path FROM files WHERE path LIKE ? ORDER BY mtime DESC LIMIT 500",
            (prefix + "%",),
        )
    else:
        rows = conn.execute("SELECT path FROM files ORDER BY mtime DESC LIMIT 500")
    for row in rows:
        yield row["path"]
