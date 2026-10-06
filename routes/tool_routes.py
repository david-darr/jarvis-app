"""Codex's way to JARVIS's tools (roadmap phase 2, 2026-10-05):
mcp_servers/hive_mind_cli.py posts each call here, and it runs through the
same registry, permission checks, lifecycle hooks and audit as every other
model's (core/tool_registry.py dispatch).

The only credential is the turn's own token (core/tool_access.py), sent as
X-JARVIS-Tool-Token, and only from this computer. Who is calling - chat,
admin or not, agent - comes from that token, never from the request.
"""
from fastapi import APIRouter, Body, HTTPException, Request

from core import tool_access, tool_registry

router = APIRouter(prefix="/api/tools", tags=["tools"])


def _context(request: Request) -> tool_registry.ToolContext:
    if not request.client or request.client.host not in ("127.0.0.1", "::1"):
        raise HTTPException(403, "Tools are reachable only from this computer")
    grant = tool_access.resolve(request.headers.get("X-JARVIS-Tool-Token", ""))
    if grant is None:
        raise HTTPException(403, "This tool access has expired; it lasts one turn")
    return tool_registry.ToolContext(session_id=grant.session_id, is_admin=grant.is_admin,
                                     turn_taint=grant.turn_taint, agent_id=grant.agent_id, model=grant.model)


@router.post("/{name}")
async def call_tool(name: str, request: Request, body: dict = Body(default={})) -> dict:
    ctx = _context(request)
    arguments = body.get("arguments") if isinstance(body, dict) else None
    return {"result": await tool_registry.dispatch(name, arguments if isinstance(arguments, dict) else {},
                                                   ctx, tool_registry.CODEX)}
