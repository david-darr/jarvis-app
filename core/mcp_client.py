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
"""
import logging
import re
from typing import Any

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


async def discover(servers: dict[str, dict]) -> dict[str, dict[str, Any]]:
    """Every tool of every reachable server, keyed by the function name the
    model will use. A server that cannot be reached is skipped and logged,
    never allowed to stop the chat."""
    found: dict[str, dict[str, Any]] = {}
    for server, config in servers.items():
        try:
            tools = await list_tools(config)
        except Exception as e:  # an unreachable server must not end the turn
            logger.warning("MCP server %r unavailable: %s", server, e)
            continue
        for tool in tools:
            name = function_name(server, tool["name"], set(found))
            found[name] = {"server": server, "config": config, **tool}
    return found
