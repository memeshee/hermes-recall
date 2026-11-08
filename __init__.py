"""recall — local file recall for Hermes Agent.

Registers five tools (`recall_index`, `recall_search`, `recall_open`, `recall_stats`,
`recall_forget`), an `@recall:` context reference, a `pre_llm_call` hook that surfaces candidate
files before the model answers, and the `/recall` slash command. Everything is local: SQLite FTS5
in the plugin's data directory, standard-library extraction, no network calls and no embedding
service. Reads go through `guard.py`: credential stores, Hermes' own home and virtual filesystems
are refused on the resolved path.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from . import hooks as _hooks
from . import refresh
from . import tools as _tools
from .refs import RecallReference
from .runtime import Runtime

logger = logging.getLogger(__name__)

TOOLSET = "recall"
SKILL_PATH = Path(__file__).resolve().parent / "skills" / "recall" / "SKILL.md"

_runtime: Runtime | None = None


def _cmd(runtime: Runtime, raw_args: str) -> str:
    args = (raw_args or "").strip()
    try:
        if not args or args in {"stats", "status"}:
            data = json.loads(_tools.recall_stats(runtime, {}))
            if "error" in data:
                return f"recall: {data['error']}"
            return (
                f"**recall** — {data.get('files', 0)} files, {data.get('chunks', 0)} chunks\n"
                f"last index: {data.get('last_index') or 'never'} · roots: {data.get('roots') or ', '.join(data.get('roots_configured') or [])}\n"
                f"ambient hints: {'on' if data.get('ambient') else 'off'} · db: {data.get('db')}"
            )
        if args.startswith("index"):
            target = args[len("index"):].strip()
            payload = {"rebuild": "--rebuild" in target}
            if target and not target.startswith("--"):
                payload["path"] = [target]
            data = json.loads(_tools.recall_index(runtime, payload))
            if "error" in data:
                return f"recall index: {data['error']}"
            return (
                f"indexed {data.get('indexed', 0)} new/changed, {data.get('unchanged', 0)} unchanged, "
                f"{data.get('skipped', 0)} skipped, {data.get('removed', 0)} removed in {data.get('seconds')}s "
                f"— {data.get('files', 0)} files total"
            )
        if args.startswith("forget"):
            target = args[len("forget"):].strip()
            if target in {"all", "*"}:
                payload = {"all": True}
            elif target:
                payload = {"path": target}
            else:
                return "usage: /recall forget <path>   or   /recall forget all"
            data = json.loads(_tools.recall_forget(runtime, payload))
            if "error" in data:
                return f"recall forget: {data['error']}"
            if data.get("cleared"):
                return f"index cleared — {data['files_removed']} files removed"
            return (
                f"removed {data.get('files_removed', 0)} file(s) from the index; "
                f"{data.get('files_remaining', 0)} remain"
            )
        data = json.loads(_tools.recall_search(runtime, {"query": args, "limit": 6}))
        if "error" in data:
            return f"recall: {data['error']}"
        results = data.get("results") or []
        if not results:
            hint = data.get("hint")
            return f"no match for “{args}”" + (f" — {hint}" if hint else "")
        lines = [f"**recall** · {len(results)} match(es) for “{args}”"]
        for hit in results:
            lines.append(
                f"- `{hit['path']}` — {hit.get('modified') or '?'} · score {hit['score']}\n  …{hit.get('snippet', '')[:160]}"
            )
        lines.append("_read one with `@recall:<path>` or the recall_open tool_")
        return "\n".join(lines)
    except Exception as exc:  # a slash command must not raise into the chat loop
        return f"recall: {exc.__class__.__name__}: {exc}"


def register(ctx) -> None:
    global _runtime
    runtime = Runtime(ctx)
    _runtime = runtime

    ctx.register_tool(
        name="recall_index",
        toolset=TOOLSET,
        schema=_tools.INDEX_SCHEMA,
        handler=lambda args, **kw: _tools.recall_index(runtime, args),
        description="Index local folders so the agent can find documents on this machine.",
        emoji="🗂️",
    )
    ctx.register_tool(
        name="recall_search",
        toolset=TOOLSET,
        schema=_tools.SEARCH_SCHEMA,
        handler=lambda args, **kw: _tools.recall_search(runtime, args),
        description="Search the user's own indexed files by keyword or phrase.",
        emoji="🔎",
    )
    ctx.register_tool(
        name="recall_open",
        toolset=TOOLSET,
        schema=_tools.OPEN_SCHEMA,
        handler=lambda args, **kw: _tools.recall_open(runtime, args),
        description="Read a local document (PDF/Office/text) as text.",
        emoji="📄",
    )
    ctx.register_tool(
        name="recall_stats",
        toolset=TOOLSET,
        schema=_tools.STATS_SCHEMA,
        handler=lambda args, **kw: _tools.recall_stats(runtime, args),
        description="Health and coverage of the local file index.",
        emoji="📊",
    )
    ctx.register_tool(
        name="recall_forget",
        toolset=TOOLSET,
        schema=_tools.FORGET_SCHEMA,
        handler=lambda args, **kw: _tools.recall_forget(runtime, args),
        description="Drop a file, folder or the whole index from local recall.",
        emoji="🧽",
    )

    ctx.register_hook("pre_llm_call", _hooks.make_hook(runtime))

    def _on_session_start(**kwargs: Any):
        # Bounded, off-thread, and a no-op until an index exists: a stale index answers "no match"
        # for a file the user just saved, so every new session nudges it forward.
        try:
            refresh.schedule(runtime)
        except Exception:
            pass
        return None

    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_context_reference(RecallReference(runtime))
    ctx.register_command(
        "recall",
        lambda raw_args="", **kw: _cmd(runtime, raw_args),
        description="Search or index your local files",
        args_hint="<query | index [path] | stats>",
    )

    try:
        ctx.register_skill(
            "recall",
            SKILL_PATH,
            description="Find, read and cite files on the user's machine with the recall index",
        )
    except Exception as exc:  # skill is a convenience, not the product
        logger.debug("recall: skill registration skipped: %s", exc)

    logger.info(
        "recall: registered 5 tools, pre_llm_call + on_session_start hooks, @recall: reference, /recall"
    )
