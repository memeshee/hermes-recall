"""Background refresh on session start: keep the index honest without the user asking.

An index that only updates when someone remembers to run a command is a stale index, and a stale
index is worse than none — it answers "no match" for a file the user just saved. So once an index
exists, every new session kicks a bounded incremental pass on a daemon thread: its own SQLite
connection (WAL, so it never blocks a turn's reads), a small file budget, and a hard wall clock.
A second session starting while one refresh is running is a no-op, and a failure is logged, never
surfaced.
"""

from __future__ import annotations

import json
import logging
import threading

from . import indexer, store

logger = logging.getLogger(__name__)

_REFRESH_LOCK = threading.Lock()
_running = False


def _worker(runtime, roots, excludes, budget_s: int, max_files: int) -> None:
    global _running
    conn = None
    try:
        conn = store.connect(runtime.db_path)
        summary = indexer.index(
            conn,
            roots,
            excludes=excludes,
            max_files=max_files,
            budget_s=budget_s,
            max_file_mb=int(runtime.setting("max_file_mb")),
            chunk_chars=int(runtime.setting("chunk_chars")),
        )
        if summary.get("indexed"):
            logger.info(
                "recall: background refresh picked up %s changed file(s) in %ss",
                summary["indexed"], summary["seconds"],
            )
    except Exception as exc:  # never let a refresh break a session start
        logger.debug("recall: background refresh failed: %s", exc)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        with _REFRESH_LOCK:
            _running = False


def refresh_roots(runtime) -> list:
    """The folders a refresh may touch: exactly what the last explicit run indexed.

    Never the configured defaults — re-resolving those here would silently widen the index the first
    time a session starts on a machine with a populated ~/Downloads, and indexing folders the user
    never asked for is the worst kind of surprise. `resolve_roots` applies the same read policy as a
    manual run, so a root that has since become unreadable simply drops out.
    """
    recorded = store.get_meta(runtime.conn, "roots_json", "")
    candidates: list[str] = []
    if recorded:
        try:
            parsed = json.loads(recorded)
        except (ValueError, TypeError):
            parsed = []
        if isinstance(parsed, list):
            candidates = [str(p).strip() for p in parsed if str(p).strip()]
    if not candidates:
        # Older indexes stored a comma-joined string; keep honouring it (a comma inside a folder
        # name is why the JSON key exists, so this path stays a fallback).
        legacy = store.get_meta(runtime.conn, "roots", "")
        candidates = [part.strip() for part in legacy.split(",") if part.strip()]
    if not candidates:
        return []
    return indexer.resolve_roots(candidates)


def schedule(runtime) -> bool:
    """Start a bounded refresh if one is not already running. Returns True when a thread started."""
    global _running
    if not runtime.setting("refresh_on_start", True):
        return False
    try:
        if store.stats(runtime.conn)["files"] == 0:
            return False  # nothing indexed yet: the first run stays an explicit user action
    except Exception:
        return False
    roots = refresh_roots(runtime)
    if not roots:
        return False
    excludes = tuple(str(x) for x in (runtime.setting("excludes") or []))
    budget = int(runtime.setting("refresh_budget_s") or 25)
    max_files = int(runtime.setting("refresh_max_files") or 5000)
    with _REFRESH_LOCK:
        if _running:
            return False
        _running = True
    thread = threading.Thread(
        target=_worker,
        args=(runtime, roots, excludes, budget, max_files),
        name="recall-refresh",
        daemon=True,
    )
    thread.start()
    return True
