"""`pre_llm_call` hook: put the likely file in front of the model before it answers.

This is the part a skill cannot do. When a message reads like it is about a document the user
already has ("send the Acme invoice to legal", "what did the lease say about parking"), the hook
looks up the local index and appends a short candidate block to the user message — paths, dates
and one highlighted line each, never the whole file. The model still has to call recall_open to
read it, so the block can only ever speed up a real lookup, not invent one.

Kept deliberately conservative: a doc-ish cue is required, at least two content tokens, a minimum
score, a per-session memory of what it already showed, and an off switch in settings.
"""

from __future__ import annotations

import os
import re
import threading
from typing import Any

from . import store

DOC_CUES = (
    "file", "files", "document", "documents", "doc", "docs", "pdf", "contract", "invoice",
    "invoices", "receipt", "receipts", "spreadsheet", "csv", "xlsx", "deck", "slides", "note",
    "notes", "letter", "resume", "cv", "screenshot", "report", "policy", "statement",
    "proposal", "quote", "spec", "lease", "agreement", "transcript", "log", "archive",
)
LOOKUP_CUES = (
    "where is", "where's", "find my", "find the", "which file", "pull up", "open the",
    "look at the", "search my", "that file", "the file", "send me the", "what did the",
    "what does the", "in my", "from my", "on my machine", "in my folder", "in my documents",
)

STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "have", "has", "you", "your", "can",
    "could", "would", "should", "please", "about", "what", "when", "where", "which", "there",
    "their", "them", "then", "than", "into", "just", "like", "make", "made", "need", "want",
    "know", "tell", "give", "take", "get", "got", "let", "lets", "how", "why", "who", "all",
    "any", "some", "not", "but", "was", "were", "are", "were", "been", "being", "did", "does",
    "doing", "done", "will", "now", "new", "old", "one", "two", "use", "using", "used", "it",
    "its", "as", "at", "by", "in", "on", "of", "or", "to", "is", "be", "if", "so", "we", "me",
    "my", "i", "a", "an", "do", "up", "out", "off", "run", "put", "see", "say", "says", "said",
}

QUOTED = re.compile(r"[\"'`]([^\"'`\n]{3,80})[\"'`]")
MIN_SCORE = 3.5
MAX_CONTEXT_CHARS = 900
_seen_lock = threading.Lock()
_seen_by_session: dict[str, set[str]] = {}


def _message_text(user_message: Any) -> str:
    if isinstance(user_message, str):
        return user_message
    if isinstance(user_message, list):
        parts: list[str] = []
        for item in user_message:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return str(user_message or "")


def _content_tokens(text: str) -> list[str]:
    out = []
    for tok in re.findall(r"[0-9A-Za-z][0-9A-Za-z._-]{1,}", text):
        low = tok.lower().strip("._-")
        if len(low) < 3 or low in STOPWORDS:
            continue
        out.append(low)
    # de-dupe, keep order
    seen, uniq = set(), []
    for tok in out:
        if tok in seen:
            continue
        seen.add(tok)
        uniq.append(tok)
    return uniq


def _already_shown(session_id: str, paths: list[str]) -> list[str]:
    with _seen_lock:
        if len(_seen_by_session) > 400:  # bound the map in a long-lived gateway
            _seen_by_session.clear()
        seen = _seen_by_session.setdefault(session_id or "-", set())
        fresh = [p for p in paths if p not in seen]
        seen.update(paths)
        return fresh


def build_context(runtime, user_message: Any, session_id: str = "") -> str:
    text = _message_text(user_message)
    if len(text.strip()) < 12 or text.lstrip().startswith("/"):
        return ""
    low = text.lower()
    quoted = QUOTED.findall(text)
    has_doc_cue = any(cue in low.split() for cue in DOC_CUES) or any(
        cue in low for cue in ("contract", "invoice", "receipt", "lease", "spreadsheet", "pdf")
    )
    has_lookup_cue = any(cue in low for cue in LOOKUP_CUES)
    if not (has_doc_cue or has_lookup_cue or quoted):
        return ""
    if "@recall:" in low or "@file:" in low or "@folder:" in low:
        return ""  # the user already pointed at it explicitly

    tokens = _content_tokens(" ".join(quoted) if quoted and len(quoted[0]) > 4 else text)
    if len(tokens) < 2:
        return ""
    query = " ".join(tokens[:8])

    conn = runtime.conn
    hits = store.search(conn, query, limit=6)
    if not hits:
        return ""
    good = [h for h in hits if h["score"] >= MIN_SCORE]
    if not good:
        return ""
    try:
        max_hints = max(1, int(runtime.setting("max_hints")))
    except Exception:
        max_hints = 3
    paths = [h["path"] for h in good[: max_hints + 2]]
    fresh = _already_shown(session_id, paths)
    picks = [h for h in good if h["path"] in fresh][:max_hints]
    if not picks:
        return ""

    lines = ["[recall] Local files in the user's index that may be the document being discussed:"]
    budget = MAX_CONTEXT_CHARS - len(lines[0])
    for hit in picks:
        name = os.path.basename(hit["path"])
        snap = re.sub(r"\s+", " ", (hit.get("snippet") or "")).strip()[:180]
        line = f"- {hit['path']} (modified {hit.get('modified') or 'unknown'}; {name}) — “{snap}”"
        if len(line) > budget:
            break
        lines.append(line)
        budget -= len(line)
    if len(lines) == 1:
        return ""
    lines.append(
        "Read one with recall_open before answering, and cite the path. If none of these is the "
        "right document, say so and ask — do not invent a file."
    )
    return "\n".join(lines)


def make_hook(runtime):
    def pre_llm_call(**kwargs: Any):
        try:
            if not runtime.setting("ambient", True):
                return None
            block = build_context(
                runtime,
                kwargs.get("user_message"),
                str(kwargs.get("session_id") or ""),
            )
            return {"context": block} if block else None
        except Exception:
            return None  # a hook must never take a turn down

    return pre_llm_call
