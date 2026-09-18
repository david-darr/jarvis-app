"""Stable account identity for shared-allowance grouping.

Two agents backed by the same provider account draw down the same allowance.
If each were given its own pool, Swarm would hand both a fictitious full
allowance and could spend the real one twice over, which is exactly the
failure the specification's shared-pool acceptance case exists to prevent.

The key is derived, never stored from a credential: an API key is reduced to
a truncated SHA-256 digest that identifies the account without being
reversible into the key itself. Nothing here is written to a transcript, a
handoff, or a log.
"""
import hashlib
from urllib.parse import urlsplit


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def account_key(endpoint: dict) -> str:
    """One opaque key per real provider account.

    `claude_cli` and `codex_cli` authenticate as the machine's own CLI login,
    so every agent using either shares one account regardless of how many
    endpoint records point at it. A hosted API is identified by host plus a
    key digest. A local server has no account allowance at all, but its
    concurrency is still shared, so it is keyed by origin.
    """
    kind = endpoint.get("kind") or "api"
    if kind in ("claude_cli", "codex_cli"):
        return kind
    origin = urlsplit(endpoint.get("base_url") or "").netloc.lower() or "unknown"
    if kind == "local":
        return f"local:{origin}"
    api_key = endpoint.get("api_key") or ""
    return f"api:{origin}:{_digest(api_key) if api_key else 'anonymous'}"


def account_label(endpoint: dict) -> str:
    """Human-readable name for the grouping, safe to display. Never a key."""
    kind = endpoint.get("kind") or "api"
    if kind == "claude_cli":
        return "Claude CLI account"
    if kind == "codex_cli":
        return "Codex CLI account"
    origin = urlsplit(endpoint.get("base_url") or "").netloc or "unknown host"
    return f"{'Local server' if kind == 'local' else 'API account'} at {origin}"
