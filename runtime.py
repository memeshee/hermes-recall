"""Runtime: one lazily opened index connection per plugin instance, plus settings access."""

from __future__ import annotations

import threading
from typing import Any

from plugins.plugin_storage import plugin_data_dir

from . import store

DEFAULTS: dict[str, Any] = {
    "ambient": True,
    "max_hints": 3,
    "max_file_mb": 8,
    "chunk_chars": 1200,
    "max_files": 20000,
    "budget_s": 90,
}


class Runtime:
    """Owns the index handle. Safe to call from the tool thread and the hook thread."""

    def __init__(self, ctx):
        self.ctx = ctx
        self._lock = threading.Lock()
        self._conn = None
        try:
            self.key = getattr(ctx, "plugin_id", "") or getattr(ctx.manifest, "name", "recall")
        except Exception:  # pragma: no cover - defensive
            self.key = "recall"

    # ---- settings -----------------------------------------------------------------
    def setting(self, key: str, default: Any = None) -> Any:
        fallback = DEFAULTS.get(key, default)
        try:
            value = self.ctx.get_config(key, fallback)
        except Exception:
            return fallback
        if value is None:
            return fallback
        return value

    # ---- storage ------------------------------------------------------------------
    @property
    def db_path(self):
        return plugin_data_dir(self.key) / "index.db"

    @property
    def conn(self):
        with self._lock:
            if self._conn is None:
                conn = store.connect(self.db_path)
                store.set_meta(conn, "db_path", str(self.db_path))
                # Commit the seed: an open implicit transaction here pins SQLite's write lock for
                # the life of this connection, so every later writer (including a reload's fresh
                # connection) fails with "database is locked".
                conn.commit()
                self._conn = conn
            return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None
