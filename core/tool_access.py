"""Per-turn tool tokens for Codex (roadmap phase 2, 2026-10-05; spec: the
vault note "Tool Registry - Phase 2 (Build Spec)").

Codex reaches JARVIS's tools by running mcp_servers/hive_mind_cli.py, which
posts to POST /api/tools/{name} (routes/tool_routes.py). Each Codex turn gets
its own token here, bound to that turn's chat: the session, whether the chat
is an admin's, the agent it belongs to, the model. The route trusts nothing
the model sends about who it is - only the token - and the token dies with
the turn (core/codex_brain.py revokes it in a finally) or after an hour.

This replaced two older credentials: the process-wide internal token, which
every Codex process held whoever was chatting, and a separate Google-only
chat token.
"""
import secrets
import time
from dataclasses import dataclass
from typing import Optional

from core.turn_taint import TurnTaint

TOKEN_LIFETIME_SECONDS = 3600


@dataclass(frozen=True)
class Grant:
    session_id: Optional[str]
    is_admin: bool
    agent_id: Optional[str]
    model: Optional[str]
    expires_at: float
    # One per turn, so reading untrusted content earlier in the turn makes
    # later sensitive tools ask, as in the other brains (core/turn_taint.py).
    turn_taint: TurnTaint


_grants: dict[str, Grant] = {}


def issue(session_id: Optional[str], is_admin: bool, agent_id: Optional[str] = None,
          model: Optional[str] = None) -> str:
    now = time.time()
    for stale in [t for t, g in _grants.items() if g.expires_at < now]:
        _grants.pop(stale, None)
    token = secrets.token_urlsafe(32)
    _grants[token] = Grant(session_id, bool(is_admin), agent_id, model, now + TOKEN_LIFETIME_SECONDS, TurnTaint())
    return token


def resolve(token: str) -> Optional[Grant]:
    grant = _grants.get(token or "")
    if grant is None or grant.expires_at < time.time():
        return None
    return grant


def revoke(token: str) -> None:
    _grants.pop(token or "", None)
