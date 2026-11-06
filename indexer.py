"""Filesystem scan for the recall index: what may be read, what is never read, and how long a run
is allowed to take.

The denylist is the safety story. It is applied before any file is opened, it covers credential
stores and agent state as well as build directories, and a user cannot remove the secret entries
by editing settings — only add more patterns.
"""

from __future__ import annotations

import fnmatch
import os
import time
from pathlib import Path
from typing import Any, Iterable

from . import store
from .extract import chunk_text, extract

# Always skipped, in addition to whatever the user configures.
SECRET_GLOBS = (
    "*.env", ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "*.keystore",
    "id_rsa*", "id_ed25519*", "id_ecdsa*", "*.kdbx", "*.asc", "*.gpg",
    ".netrc", ".pgpass", ".git-credentials", "credentials", "credentials.json",
    "auth.json", "secrets.json", "*.secret", ".npmrc", ".pypirc", "state.db*",
)

# Always skipped directories (matched against the directory name).
SKIP_DIRS = (
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", "env",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".next", ".nuxt", "dist",
    "build", "target", ".gradle", ".idea", ".vscode", "site-packages", ".cache",
    ".Trash", "Trash", "$RECYCLE.BIN", "System Volume Information",
    ".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", ".password-store",
    "Library", "AppData", ".local",
)

DEFAULT_ROOTS = ("~/Documents", "~/Desktop", "~/Downloads", "~/Projects")

MAX_CHUNKS_PER_FILE = 400


def _expand(p: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(p))))


def resolve_roots(configured: Iterable[str] | None) -> list[Path]:
    raw = [str(r) for r in (configured or []) if str(r).strip()] or list(DEFAULT_ROOTS)
    roots: list[Path] = []
    for item in raw:
        path = _expand(item)
        if path.is_dir():
            roots.append(path.resolve())
    return roots


def is_secret(path: Path) -> bool:
    name = path.name.lower()
    for pattern in SECRET_GLOBS:
        if fnmatch.fnmatch(name, pattern):
            return True
    parts = {p.lower() for p in path.parts}
    if parts & {".ssh", ".gnupg", ".aws", ".kube", ".password-store", ".azure"}:
        return True
    low = str(path).lower()
    # Never index another profile's Hermes secrets or the agent's own session store.
    if "/.hermes/" in low and ("/.env" in low or low.endswith("auth.json") or "state.db" in low):
        return True
    return False


def _user_excluded(path: Path, patterns: tuple[str, ...]) -> bool:
    if not patterns:
        return False
    low = str(path).lower()
    name = path.name.lower()
    for pattern in patterns:
        pat = pattern.lower()
        if fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(low, pat) or pat in low:
            return True
    return False


def walk_files(roots: list[Path], excludes: tuple[str, ...] = ()) -> Iterable[Path]:
    seen_roots = tuple(str(r) for r in roots)
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = [
                d for d in dirnames
                if d not in SKIP_DIRS and not d.startswith(".git") and not _user_excluded(Path(dirpath) / d, excludes)
            ]
            for name in filenames:
                path = Path(dirpath) / name
                if is_secret(path) or _user_excluded(path, excludes):
                    continue
                # Guard against a symlinked root escaping into somewhere unexpected.
                try:
                    real = str(path.resolve())
                except OSError:
                    continue
                if not real.startswith(seen_roots):
                    continue
                yield path


def index(
    conn,
    roots: list[Path],
    *,
    excludes: tuple[str, ...] = (),
    max_files: int = 20000,
    budget_s: int = 90,
    max_file_mb: int = 8,
    chunk_chars: int = 1200,
    force: bool = False,
) -> dict[str, Any]:
    started = time.time()
    deadline = started + max(5, int(budget_s))
    max_bytes = max(1, int(max_file_mb)) * 1024 * 1024
    touched = unchanged = skipped_n = errors = 0
    seen: set[str] = set()
    reason_counts: dict[str, int] = {}

    for path in walk_files(roots, excludes):
        if time.time() > deadline:
            break
        if touched + unchanged >= max_files:
            break
        spath = str(path)
        if spath in seen:
            continue
        seen.add(spath)
        try:
            st = path.stat()
        except OSError:
            continue
        row = store.file_state(conn, spath)
        if row and not force and abs(row["mtime"] - st.st_mtime) < 1e-6 and row["size"] == st.st_size:
            unchanged += 1
            continue
        text, note = extract(path, max_bytes)
        # A name/synthetic chunk is always indexed, so a scanned PDF or an image is still findable
        # by the words in its filename even though its content cannot be read.
        name_chunk = f"file {path.stem} {path.parent.name} {path.suffix.lstrip('.')}"
        if not text:
            skipped_n += 1
            key = note.split(":")[0] or "unknown"
            reason_counts[key] = reason_counts.get(key, 0) + 1
            store.put_file(
                conn, spath, st.st_mtime, st.st_size, path.suffix.lower(),
                [name_chunk], note,
            )
            touched += 1
            if touched % 200 == 0:
                conn.commit()
            continue
        chunks = [name_chunk] + chunk_text(text, chunk_chars)[:MAX_CHUNKS_PER_FILE]
        try:
            store.put_file(conn, spath, st.st_mtime, st.st_size, path.suffix.lower(), chunks, "")
        except Exception:  # a corrupt file must not abort the whole run
            errors += 1
            continue
        touched += 1
        if touched % 100 == 0:
            conn.commit()

    # Drop entries whose file disappeared from a scanned root.
    removed = 0
    for row in conn.execute("SELECT path FROM files").fetchall():
        p = row["path"]
        if p in seen:
            continue
        if not any(p.startswith(str(r)) for r in roots):
            continue
        if not Path(p).exists():
            store.drop_file(conn, p)
            removed += 1

    elapsed = time.time() - started
    store.set_meta(conn, "last_index", time.strftime("%Y-%m-%d %H:%M:%S"))
    store.set_meta(conn, "roots", ", ".join(str(r) for r in roots))
    store.set_meta(conn, "index_seconds", f"{elapsed:.1f}")
    store.set_meta(conn, "db_path", str(getattr(conn, "_recall_db_path", "")))
    conn.commit()
    return {
        "indexed": touched,
        "unchanged": unchanged,
        "skipped": skipped_n,
        "removed": removed,
        "errors": errors,
        "skip_reasons": dict(sorted(reason_counts.items(), key=lambda kv: -kv[1])[:6]),
        "seconds": round(elapsed, 2),
        "partial": elapsed >= max(5, int(budget_s)) - 0.5,
        "roots": [str(r) for r in roots],
    }
