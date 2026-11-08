"""Path policy: the one place that decides whether recall may read a path.

Every read path in the plugin — the indexer's walk, `recall_open`, and the `@recall:` expansion —
asks this module before it opens anything, and it asks about the **resolved** path so a symlink
cannot launder a secret through an innocent name (`notes.txt` → `~/.ssh/id_rsa`).

Three rules, in order:

1. **Hermes' own home is never read.** It holds the vault, session transcripts, MCP OAuth tokens
   (`mcp-tokens/`), webhook signing secrets (`webhook_subscriptions.json`), the dashboard session
   token, provider credentials and this plugin's own index. A document assistant has no business
   quoting them, and an ambient hint that quoted one would put it in the model prompt unasked.
   Both the live home (`hermes_constants.get_hermes_home()`, `$HERMES_HOME`) and `~/.hermes` are
   blocked, because a profile-routed process can see one while the other still exists on disk.
2. **Virtual filesystems are never read.** `/proc/self/environ` is a readable file whose contents
   are the process environment — every API key in it.
3. **The credential-store denylist** applies to the resolved name and to the resolved parent path.

Name matching can never be complete: a password written inside `meeting-notes.txt` is still a
document. The rule that actually holds is the first one — the agent's own secrets and state are
unreachable, no matter what a file is called.
"""

from __future__ import annotations

import fnmatch
import os
import threading
from pathlib import Path
from typing import Iterable

# Always skipped, in addition to whatever the user configures.
SECRET_GLOBS = (
    "*.env", ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "*.keystore",
    "id_rsa*", "id_ed25519*", "id_ecdsa*", "*.kdbx", "*.asc", "*.gpg",
    ".netrc", ".pgpass", ".git-credentials", "credentials", "credentials.json",
    "auth.json", "secrets.json", "*.secret", ".npmrc", ".pypirc", "state.db*",
    # Secret-bearing names people actually use in Documents/Downloads. Name matching can never be
    # complete — a password in `notes.txt` still gets indexed — but these are the habitual ones
    # (recovery codes, seed phrases, API tokens, exported keys) and indexing them is indefensible.
    "key.txt", "keys.txt", "tokens.json", "api.json", "*apikey*", "*api*key*",
    "*recovery*code*", "*backup*code*", "*seed*phrase*", "*mnemonic*", "*keystore*",
    "*secret*", "*credential*", "*.ovpn", "*.ppk", "wallet*.dat", "*.wallet",
    # Hermes/agent state that must stay unreadable even when it is copied out of the home directory.
    "mcp-tokens", "mcp-tokens.json", "mcp_tokens.json", "webhook_subscriptions.json",
    "dashboard_session*", "session_token*", "*.session-token", "*_key.json", "*_keys.json",
    "agent_key.json", "technocore_agent_key.json", "vault.json", ".htpasswd",
    "keepass*", "*.psafe3", "*.kdb",
)

# Never descended into and never read (virtual or device filesystems).
VIRTUAL_ROOTS = ("/proc", "/sys", "/dev", "/run/user")

# A resolved path may not be one of these directories either, by name.
SECRET_DIR_PARTS = {
    ".ssh", ".gnupg", ".aws", ".kube", ".password-store", ".azure", "mcp-tokens",
    ".docker", ".config",  # .config holds gcloud/gh/npm auth on a desktop
}

_homes_lock = threading.Lock()
_homes_cache: dict[str, tuple[Path, ...]] = {}


def expand(path: str | Path) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(path))))


def resolve(path: str | Path) -> Path:
    """Realpath of a path, tolerating files that do not exist (Python <3.10 style)."""
    p = expand(path)
    try:
        return Path(os.path.realpath(p))
    except OSError:  # pragma: no cover - defensive
        return p


def is_relative_to(path: Path, root: Path) -> bool:
    """Component-wise containment. `/a/bc` is **not** inside `/a/b` (a prefix test says it is)."""
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def hermes_homes() -> tuple[Path, ...]:
    """Every directory that must be unreachable: the live Hermes home and `~/.hermes`.

    Cached per `$HERMES_HOME` value because profile routing can change it between turns while the
    indexer calls this once per file. The home itself is never a legal answer (`/` would block
    everything), so shallow or missing paths are dropped.
    """
    key = os.environ.get("HERMES_HOME", "")
    cached = _homes_cache.get(key)
    if cached is not None:
        return cached

    candidates: list[Path] = []
    try:  # the authoritative answer when the plugin runs inside a host
        from hermes_constants import get_hermes_home  # type: ignore

        candidates.append(Path(get_hermes_home()))
    except Exception:
        pass
    if key.strip():
        candidates.append(Path(key.strip()))
    candidates.append(Path.home() / ".hermes")

    homes: list[Path] = []
    for candidate in candidates:
        real = resolve(candidate)
        if len(real.parts) < 3:  # '/', '/home', or a bare top-level dir: refuse to treat as home
            continue
        if real not in homes:
            homes.append(real)
    with _homes_lock:
        _homes_cache[key] = tuple(homes)
    return tuple(homes)


def forget_homes_cache() -> None:
    with _homes_lock:
        _homes_cache.clear()


def is_secret(path: str | Path) -> bool:
    """True when the *given* path's name or parents match the credential-store denylist."""
    p = expand(path)
    name = p.name.lower()
    for pattern in SECRET_GLOBS:
        if fnmatch.fnmatch(name, pattern):
            return True
    parts = {part.lower() for part in p.parts}
    if parts & SECRET_DIR_PARTS:
        return True
    low = str(p).lower()
    # Never index another profile's Hermes secrets or the agent's own session store.
    if "/.hermes/" in low and (
        "/.env" in low or low.endswith("auth.json") or "state.db" in low or "/sessions/" in low
    ):
        return True
    return False


def _home_reason(real: Path, given: Path) -> str:
    for home in hermes_homes():
        if is_relative_to(real, home) or is_relative_to(given, home):
            return (
                f"refused: inside the Hermes home ({home}) — vault, session transcripts, "
                "MCP tokens and this plugin's own index live there"
            )
    return ""


def _virtual_reason(real: Path) -> str:
    for vroot in VIRTUAL_ROOTS:
        if is_relative_to(real, Path(vroot)):
            return f"refused: virtual filesystem ({vroot}) — /proc and /sys expose the process, not documents"
    return ""


def inspect(path: str | Path) -> tuple[Path, str]:
    """Return `(resolved_path, refusal_reason)`. An empty reason means the path may be read."""
    given = expand(path)
    real = resolve(given)
    reason = _home_reason(real, given)
    if reason:
        return real, reason
    reason = _virtual_reason(real)
    if reason:
        return real, reason
    if is_secret(real) or is_secret(given):
        return real, "refused: path matches the credential-store denylist"
    return real, ""


def inspect_dir(path: str | Path) -> tuple[Path, str]:
    """Directory version of `inspect`: hard rules only.

    The filename denylist is deliberately *not* applied to directories, so a folder that happens to
    be called `secret-plans/` is still walked; its files are judged one by one.
    """
    given = expand(path)
    real = resolve(given)
    reason = _home_reason(real, given) or _virtual_reason(real)
    if reason:
        return real, reason
    if {part.lower() for part in real.parts} & SECRET_DIR_PARTS:
        return real, "refused: directory is a credential store"
    return real, ""


def refusal_for_roots(roots: Iterable[str | Path]) -> dict[str, str]:
    """Map of configured root -> refusal reason, for roots the plugin will not index."""
    out: dict[str, str] = {}
    for root in roots:
        _, reason = inspect_dir(root)
        if reason:
            out[str(root)] = reason
    return out
