"""Filesystem scan for the recall index: what may be read, what is never read, and how long a run
is allowed to take.

Path policy lives in `guard.py` and is asked *before* any file is opened, on the resolved path. It
covers the credential-store denylist, Hermes' own home (vault, sessions, MCP tokens, webhooks, the
index itself) and virtual filesystems, and a user cannot remove those rules by editing settings —
only add more patterns.
"""

from __future__ import annotations

import fnmatch
import json
import os
import time
from pathlib import Path
from typing import Any, Iterable

from . import guard, store
from .extract import chunk_text, extract
from .guard import SECRET_GLOBS, is_secret  # re-exported: the denylist is part of this module's API

# Always skipped directories (matched against the directory name).
SKIP_DIRS = (
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", "env",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".next", ".nuxt", "dist",
    "build", "target", ".gradle", ".idea", ".vscode", "site-packages", ".cache",
    ".Trash", "Trash", "$RECYCLE.BIN", "System Volume Information",
    ".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", ".password-store",
    "Library", "AppData", ".local",
)

# Conservative on purpose: one folder that holds documents, not the whole home directory. ~/Downloads
# and ~/Desktop are full of screenshots, installers, bank statements and exported keys, so they are
# opt-in (name them in the roots setting) rather than indexed by default.
DEFAULT_ROOTS = ("~/Documents",)

MAX_CHUNKS_PER_FILE = 400


def _expand(p: str) -> Path:
    return guard.expand(p)


def resolve_roots(configured: Iterable[str] | None) -> list[Path]:
    """Folders this plugin may index, resolved, deduplicated, and with refused roots dropped.

    A root inside Hermes' home is refused rather than silently walked: `recall_index path=["~"]` is
    a legitimate thing for a user to type, and it must not index `~/.hermes`. A bare string is
    accepted too — `hermes config set` happily stores one.
    """
    raw = _as_list(configured) or list(DEFAULT_ROOTS)
    roots: list[Path] = []
    for item in raw:
        path = _expand(item)
        if not path.is_dir():
            continue
        real, reason = guard.inspect_dir(path)
        if reason:
            continue
        # A root already covered by an earlier one would only be walked twice.
        if any(guard.is_relative_to(real, existing) for existing in roots):
            continue
        roots.append(real)
    return roots


def refused_roots(configured: Iterable[str] | None) -> dict[str, str]:
    """Configured roots that exist but may not be indexed, with the reason, for a readable error."""
    raw = [r for r in _as_list(configured) if _expand(r).is_dir()]
    return guard.refusal_for_roots(raw)


def _as_list(configured: Iterable[str] | None) -> list[str]:
    """A single path string is one root, not a sequence of characters."""
    if configured is None:
        return []
    if isinstance(configured, (str, os.PathLike)):
        configured = [configured]
    return [str(r).strip() for r in configured if str(r).strip()]


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


def walk_files(
    roots: list[Path],
    excludes: tuple[str, ...] = (),
    refusals: dict[str, int] | None = None,
) -> Iterable[Path]:
    """Yield readable files under `roots`.

    Every directory and file is judged on its **resolved** path (`guard.inspect*`), so a symlink
    named `notes.txt` cannot point at a credential store, and containment is checked component by
    component so `~/Documents-private` is not treated as being inside `~/Documents`.
    """
    for root in roots:
        root_real = guard.resolve(root)
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            keep: list[str] = []
            for name in dirnames:
                child = Path(dirpath) / name
                if name in SKIP_DIRS or name.startswith(".git") or _user_excluded(child, excludes):
                    continue
                _, reason = guard.inspect_dir(child)
                if reason:
                    if refusals is not None:
                        refusals[reason.split(" (")[0]] = refusals.get(reason.split(" (")[0], 0) + 1
                    continue
                keep.append(name)
            dirnames[:] = keep
            for name in filenames:
                path = Path(dirpath) / name
                if _user_excluded(path, excludes):
                    continue
                real, reason = guard.inspect(path)
                if reason:
                    if refusals is not None:
                        refusals[reason.split(" (")[0]] = refusals.get(reason.split(" (")[0], 0) + 1
                    continue
                if not guard.is_relative_to(real, root_real):
                    continue  # a symlink that leaves the root
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
    refusals: dict[str, int] = {}

    for path in walk_files(roots, excludes, refusals):
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
        # Component-wise: a prefix test would treat ~/Documents-private as inside ~/Documents.
        if not any(guard.is_relative_to(guard.expand(p), guard.resolve(r)) for r in roots):
            continue
        if not Path(p).exists():
            store.drop_file(conn, p)
            removed += 1

    elapsed = time.time() - started
    store.set_meta(conn, "last_index", time.strftime("%Y-%m-%d %H:%M:%S"))
    store.set_meta(conn, "roots", ", ".join(str(r) for r in roots))
    # JSON as well as the human-readable line: a folder may legally contain a comma, and the
    # background refresh reads this back to decide what it may touch.
    store.set_meta(conn, "roots_json", json.dumps([str(r) for r in roots]))
    store.set_meta(conn, "index_seconds", f"{elapsed:.1f}")
    db_path = str(getattr(conn, "_recall_db_path", "") or store.get_meta(conn, "db_path", ""))
    if db_path:
        store.set_meta(conn, "db_path", db_path)
    conn.commit()
    return {
        "indexed": touched,
        "unchanged": unchanged,
        "skipped": skipped_n,
        "removed": removed,
        "errors": errors,
        "skip_reasons": dict(sorted(reason_counts.items(), key=lambda kv: -kv[1])[:6]),
        "refused": dict(sorted(refusals.items(), key=lambda kv: -kv[1])[:6]),
        "seconds": round(elapsed, 2),
        "partial": elapsed >= max(5, int(budget_s)) - 0.5,
        "roots": [str(r) for r in roots],
    }
