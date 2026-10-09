"""JARVIS's own MCP client, for models that have none (Hermes track 2026-09-23).

Claude Code connects to MCP servers itself (core/brain.py hands it the
registered ones). OpenAI-compatible models - local and API - had no way to
use them at all; this gives them the same servers, through the official `mcp`
package's client.

Each listing and each call opens its own short connection rather than one
held for the whole chat: a connection lives inside an async task scope, and a
chat's turns run in different request tasks, so a long-lived one could not
be closed where it was opened. Servers are the same runtime configs Claude
gets (core/integrations.list_mcp_servers_runtime): http with an optional
bearer key, or a stdio command.

Every listing is also a health check (roadmap phase 6, 2026-10-06): its
result is kept on the integration (core/integrations.record_check), and a
tool held for review there - new or changed since it was pinned - is left
out of what a chat is offered.
"""
import logging
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

LIST_TIMEOUT_SECONDS = 20
CALL_TIMEOUT_SECONDS = 120
_NAME_OK = re.compile(r"[^a-zA-Z0-9_-]")


def _target(config: dict):
    from mcp.client.stdio import StdioServerParameters
    if config.get("type") == "stdio":
        return StdioServerParameters(command=config["command"], args=list(config.get("args") or []),
                                     env=config.get("env") or None)
    headers = config.get("headers") or {}
    if not headers:
        return config["url"]
    import httpx2
    from mcp.client.streamable_http import streamable_http_client
    return streamable_http_client(config["url"], http_client=httpx2.AsyncClient(headers=headers, timeout=CALL_TIMEOUT_SECONDS))


def _client(config: dict, timeout: float):
    from mcp.client import Client
    return Client(_target(config), read_timeout_seconds=timeout)


async def list_tools(config: dict) -> list[dict]:
    """The server's tools: {"name", "description", "schema"}."""
    async with _client(config, LIST_TIMEOUT_SECONDS) as client:
        result = await client.list_tools()
    return [{"name": t.name, "description": t.description or "",
             "schema": t.input_schema or {"type": "object", "properties": {}}} for t in result.tools]


async def call_tool(config: dict, tool: str, arguments: dict) -> str:
    """Call one tool and return what the model reads: its text content, with
    anything else named rather than dropped silently."""
    async with _client(config, CALL_TIMEOUT_SECONDS) as client:
        result = await client.call_tool(tool, arguments or {})
    parts = []
    for block in result.content or []:
        text = getattr(block, "text", None)
        parts.append(text if text is not None else f"[{getattr(block, 'type', 'non-text')} content not shown]")
    text = "\n".join(parts) or "(the tool returned nothing)"
    return f"Tool error from the server: {text}" if getattr(result, "is_error", False) else text


def function_name(server: str, tool: str, taken: set[str]) -> str:
    """mcp__<server>__<tool>, as Claude names them, made safe for OpenAI's
    function-name rules (letters, digits, _ and -, at most 64) and unique."""
    base = _NAME_OK.sub("_", f"mcp__{server}__{tool}")[:64]
    name, n = base, 2
    while name in taken:
        suffix = f"_{n}"
        name, n = base[:64 - len(suffix)] + suffix, n + 1
    return name


def _record(**kwargs) -> None:
    """Keep a check's result; never allowed to stop the chat that made it."""
    from core import integrations
    try:
        integrations.record_check(**kwargs)
    except Exception:
        logger.exception("could not record an MCP check")


async def discover(servers: dict[str, dict]) -> dict[str, dict[str, Any]]:
    """Every tool of every reachable server, keyed by the function name the
    model will use, except those held for review. A server that cannot be
    reached is skipped and its status says so; it never stops the chat."""
    from core import integrations
    found: dict[str, dict[str, Any]] = {}
    for server, config in servers.items():
        try:
            tools = await list_tools(config)
        except Exception as e:  # an unreachable server must not end the turn
            logger.warning("MCP server %r unavailable: %s", server, e)
            _record(name=server, error=f"{type(e).__name__}: {e}")
            continue
        _record(name=server, tools=tools)
        held = integrations.held_tools().get(server, set())
        for tool in tools:
            if tool["name"] in held:
                continue
            name = function_name(server, tool["name"], set(found))
            found[name] = {"server": server, "config": config, **tool}
    return found


async def check(item_id: str) -> Optional[dict]:
    """Check one MCP server now (Tool Store's Check, adding a server, the
    task loop): its status, pins and held tools, as the masked record. None
    for an unknown server."""
    from core import integrations, mcp_oauth
    item = integrations.get_integration(item_id)
    if item is None or item.get("kind") != "mcp_server":
        return None
    if not item.get("enabled", True):
        return integrations.get_integration_masked(item_id)
    await mcp_oauth.refresh_due([item_id])
    config = integrations.list_mcp_servers_runtime([item_id]).get(item["name"])
    if config is None:  # an OAuth server not signed in is left out until it is
        return integrations.record_check(item_id, signed_out=True)
    try:
        tools = await list_tools(config)
    except Exception as e:
        logger.warning("MCP server %r check failed: %s", item["name"], e)
        return integrations.record_check(item_id, error=f"{type(e).__name__}: {e}")
    return integrations.record_check(item_id, tools=tools)


async def check_all() -> int:
    """Check every MCP server; how many. The task loop's regular pass, which
    is how a change reaches Claude chats: Claude Code lists a server's tools
    itself and never tells JARVIS what it found."""
    from core import integrations
    ids = [i["id"] for i in integrations.list_integrations() if i["kind"] == "mcp_server"]
    for item_id in ids:
        try:
            await check(item_id)
        except Exception:
            logger.exception("MCP check of %s failed", item_id)
    return len(ids)
