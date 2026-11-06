"""Tool handlers. Every handler returns a JSON string and never raises."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import indexer, store
from .extract import extract

OPEN_CHAR_CAP = 24000


def _err(message: str, **extra: Any) -> str:
    return json.dumps({"error": message, **extra}, ensure_ascii=False)


def recall_index(runtime, args: dict[str, Any]) -> str:
    try:
        conn = runtime.conn
        requested = args.get("path") or args.get("paths")
        if isinstance(requested, str):
            requested = [requested]
        roots = indexer.resolve_roots(requested or runtime.setting("roots"))
        if not roots:
            return _err(
                "no indexable folders found",
                configured=runtime.setting("roots"),
                hint="pass path=[\"/abs/dir\"] or set plugins.entries.recall.settings.roots",
            )
        summary = indexer.index(
            conn,
            roots,
            excludes=tuple(str(x) for x in (runtime.setting("excludes") or [])),
            max_files=int(args.get("max_files") or runtime.setting("max_files")),
            budget_s=int(args.get("budget_s") or runtime.setting("budget_s")),
            max_file_mb=int(runtime.setting("max_file_mb")),
            chunk_chars=int(runtime.setting("chunk_chars")),
            force=bool(args.get("rebuild")),
        )
        summary.update(store.stats(conn))
        return json.dumps(summary, ensure_ascii=False)
    except Exception as exc:  # never break a turn
        return _err(f"index failed: {exc.__class__.__name__}: {exc}")


def recall_search(runtime, args: dict[str, Any]) -> str:
    try:
        query = str(args.get("query") or "").strip()
        if not query:
            return _err("query is required")
        limit = max(1, min(int(args.get("limit") or 8), 25))
        conn = runtime.conn
        total = store.stats(conn)["files"]
        if total == 0:
            return json.dumps(
                {
                    "results": [],
                    "index": {"files": 0},
                    "hint": "the index is empty — call recall_index once (it is incremental after that)",
                },
                ensure_ascii=False,
            )
        results = store.search(
            conn,
            query,
            limit=limit,
            path_filter=str(args.get("path") or args.get("path_filter") or ""),
            ext=str(args.get("ext") or ""),
        )
        return json.dumps(
            {"query": query, "results": results, "count": len(results), "indexed_files": total},
            ensure_ascii=False,
        )
    except Exception as exc:
        return _err(f"search failed: {exc.__class__.__name__}: {exc}")


def _read_any(path: Path, cap: int) -> tuple[str, str]:
    text, note = extract(path, max_bytes=64 * 1024 * 1024)
    if text:
        return text, note
    return "", note


def recall_open(runtime, args: dict[str, Any]) -> str:
    try:
        raw = str(args.get("path") or "").strip()
        if not raw:
            return _err("path is required")
        path = Path(raw).expanduser()
        if not path.is_absolute():
            # Accept a result path returned by recall_search relative to the home dir as well.
            path = Path.home() / path
        if not path.exists():
            return _err(f"not found: {path}")
        if not path.is_file():
            return _err(f"not a file: {path}")
        if indexer.is_secret(path):
            return _err("refused: path matches the credential-store denylist")
        cap = max(500, min(int(args.get("max_chars") or OPEN_CHAR_CAP), 120000))
        offset = max(0, int(args.get("offset") or 0))
        text, note = _read_any(path, cap)
        if not text:
            return _err(f"no readable text ({note or 'unknown'})", path=str(path))
        window = text[offset: offset + cap]
        return json.dumps(
            {
                "path": str(path),
                "chars_total": len(text),
                "offset": offset,
                "returned": len(window),
                "truncated": offset + len(window) < len(text),
                "note": note,
                "text": window,
            },
            ensure_ascii=False,
        )
    except Exception as exc:
        return _err(f"open failed: {exc.__class__.__name__}: {exc}")


def recall_stats(runtime, args: dict[str, Any]) -> str:
    try:
        conn = runtime.conn
        out = store.stats(conn)
        out["roots_configured"] = runtime.setting("roots") or list(indexer.DEFAULT_ROOTS)
        out["ambient"] = bool(runtime.setting("ambient"))
        return json.dumps(out, ensure_ascii=False)
    except Exception as exc:
        return _err(f"stats failed: {exc.__class__.__name__}: {exc}")


INDEX_SCHEMA = {
    "name": "recall_index",
    "description": (
        "Build or refresh the local file index that recall_search and recall_open read. "
        "Incremental: only new or changed files are re-extracted, so repeat calls are cheap. "
        "Credential stores (.env, *.pem, id_rsa, .ssh, .aws, browser and agent auth files) and "
        "build directories are never read. Use this before searching a folder for the first time, "
        "or when the user says a file is missing from results."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Absolute folders to index. Defaults to the configured roots (~/Documents, ~/Desktop, ~/Downloads, ~/Projects).",
            },
            "rebuild": {"type": "boolean", "description": "Re-extract every file even if unchanged."},
            "budget_s": {"type": "integer", "description": "Wall-clock budget for this run, in seconds. Partial runs resume next call."},
            "max_files": {"type": "integer", "description": "Stop after this many files this run."},
        },
    },
}

SEARCH_SCHEMA = {
    "name": "recall_search",
    "description": (
        "Search the user's own files by meaning-bearing keywords (BM25 over the local index). "
        "Use for 'where is that document about X', 'find the invoice from Acme', 'which file "
        "mentions the delivery date'. Returns path, modified time, a highlighted snippet and a "
        "score. Follow up with recall_open on the best hit before quoting anything."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Keywords or a natural phrase describing the document."},
            "limit": {"type": "integer", "description": "Max results (default 8, max 25)."},
            "path": {"type": "string", "description": "Only return files whose path contains this substring."},
            "ext": {"type": "string", "description": "Only return this extension, e.g. pdf or md."},
        },
        "required": ["query"],
    },
}

OPEN_SCHEMA = {
    "name": "recall_open",
    "description": (
        "Read a file that recall_search returned (or any local document) as text: PDF, Word, "
        "Excel, PowerPoint, Markdown, code, CSV. Supports offset/max_chars for long documents so "
        "you can page through instead of truncating silently."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute path (or a path returned by recall_search)."},
            "offset": {"type": "integer", "description": "Character offset to start at (default 0)."},
            "max_chars": {"type": "integer", "description": "Characters to return (default 24000)."},
        },
        "required": ["path"],
    },
}

STATS_SCHEMA = {
    "name": "recall_stats",
    "description": "Index health for the local file recall index: file and chunk counts, last run, roots, skip reasons.",
    "parameters": {"type": "object", "properties": {}},
}
