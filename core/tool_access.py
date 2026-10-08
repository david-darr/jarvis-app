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
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from core.constants import DATA_DIR
from core.turn_taint import TurnTaint

TOKEN_LIFETIME_SECONDS = 3600
SCREEN_ROOT = Path(DATA_DIR) / "computer" / "screens"
SCREEN_MAX_AGE_SECONDS = 86400


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
    screenshot_dir: str | None = None


_grants: dict[str, Grant] = {}


def issue(session_id: Optional[str], is_admin: bool, agent_id: Optional[str] = None,
          model: Optional[str] = None) -> str:
    now = time.time()
    for stale in [t for t, g in _grants.items() if g.expires_at < now]:
        revoke(stale)
    token = secrets.token_urlsafe(32)
    folder = str(SCREEN_ROOT / secrets.token_hex(8))
    _grants[token] = Grant(session_id, bool(is_admin), agent_id, model, now + TOKEN_LIFETIME_SECONDS,
                           TurnTaint(), folder)
    return token


def resolve(token: str) -> Optional[Grant]:
    grant = _grants.get(token or "")
    if grant is None or grant.expires_at < time.time():
        return None
    return grant


def revoke(token: str) -> None:
    grant = _grants.pop(token or "", None)
    if grant and grant.screenshot_dir:
        folder = safe_screenshot_dir(grant)
        if folder is None:
            return
        shutil.rmtree(folder, ignore_errors=True)


def safe_screenshot_dir(grant: Grant) -> Path | None:
    """Resolve a turn folder inside Kairos data before writing or deleting."""
    if not grant.screenshot_dir:
        return None
    folder = Path(grant.screenshot_dir)
    if not re.fullmatch(r"[0-9a-f]{16}", folder.name):
        return None
    root = SCREEN_ROOT.resolve()
    if Path(DATA_DIR).resolve() not in root.parents or folder.parent.resolve() != root:
        return None
    resolved = folder.resolve()
    return folder if root in resolved.parents else None


def sweep_screens() -> None:
    """Remove old turn images left behind by a crashed app."""
    if not SCREEN_ROOT.exists():
        return
    cutoff = time.time() - SCREEN_MAX_AGE_SECONDS
    for folder in SCREEN_ROOT.iterdir():
        if not folder.is_dir() or not re.fullmatch(r"[0-9a-f]{16}", folder.name):
            continue
        if folder.stat().st_mtime >= cutoff:
            continue
        grant = Grant(None, False, None, None, 0, TurnTaint(), str(folder))
        if safe_screenshot_dir(grant) == folder:
            shutil.rmtree(folder, ignore_errors=True)
