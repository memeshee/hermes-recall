"""`@recall:` context reference: the typed form of the same index, expanded inline into the prompt.

Accepts either an absolute path (`@recall:/home/me/Documents/acme-2026-03.pdf`) or a query
(`@recall:acme invoice march`). A query expands to the top few matching chunks with their paths
attached, so the model can quote the file instead of describing where it might be.
"""

from __future__ import annotations

import os
from pathlib import Path

from agent.context_references import ContextCompletionItem, ContextReferenceProvider

from . import indexer, store
from .extract import extract

EXPAND_CHAR_CAP = 6000
SINGLE_FILE_CAP = 8000


class RecallReference(ContextReferenceProvider):
    prefix = "recall"
    description = "your indexed local files"

    def __init__(self, runtime):
        self.rt = runtime

    async def autocomplete(self, query: str, *, limit: int = 10) -> list[ContextCompletionItem]:
        try:
            q = (query or "").strip()
            if not q:
                return [
                    ContextCompletionItem(text=p, display=os.path.basename(p), meta="recent")
                    for p in list(store.iter_paths(self.rt.conn, ""))[:limit]
                ]
            hits = store.search(self.rt.conn, q, limit=limit)
            return [
                ContextCompletionItem(
                    text=h["path"],
                    display=h["name"],
                    meta=f"{h.get('modified', '')} · score {h['score']}",
                )
                for h in hits
            ]
        except Exception:
            return []

    async def expand(self, target: str) -> str | None:
        target = (target or "").strip().strip("`\"'")
        if not target:
            return None
        if os.path.isabs(os.path.expanduser(target)):
            path = Path(os.path.expanduser(target))
            if not path.is_file() or indexer.is_secret(path):
                return None
            text, note = extract(path, max_bytes=64 * 1024 * 1024)
            if not text:
                return f"{path}\n\n(no readable text: {note})"
            body = text[:SINGLE_FILE_CAP]
            more = "" if len(text) <= SINGLE_FILE_CAP else f"\n\n… {len(text) - SINGLE_FILE_CAP} more characters (use recall_open for the rest)"
            return f"LOCAL FILE {path}\n\n{body}{more}"
        hits = store.search(self.rt.conn, target, limit=3)
        if not hits:
            return f"No indexed local file matches “{target}”. Call recall_index if the folder has never been indexed."
        parts = [f"LOCAL FILES matching “{target}” (from the local recall index):"]
        budget = EXPAND_CHAR_CAP
        for hit in hits:
            chunk = (hit.get("snippet") or "").strip()
            head = f"\n\n--- {hit['path']} (modified {hit.get('modified', 'unknown')}, score {hit['score']}) ---\n"
            body = chunk[: max(0, budget - len(head))]
            if not body:
                break
            parts.append(head + body)
            budget -= len(head) + len(body)
        parts.append("\n\nUse recall_open on a path to read the whole document before quoting it.")
        return "".join(parts)
