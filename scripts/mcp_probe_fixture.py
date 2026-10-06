"""A real stdio MCP server for scripts/test_integrations.py (roadmap phase 6)
and the phase's live check.

    python mcp_probe_fixture.py <spec.json>

The spec file is read at every start and lists the tools to offer:
{"tools": [{"name": ..., "description": ...}]}, so a test can change a
server's tools between checks. Each tool answers with its own name. One
more tool, `env_names`, returns the names (never the values) of the
environment variables this server was started with, to prove what reached
it.
"""
import json
import os
import sys

from mcp.server.mcpserver import MCPServer

server = MCPServer("probe")


def env_names() -> str:
    """The names of the environment variables this server received."""
    return " ".join(sorted(os.environ))


def _make(name: str):
    def tool(text: str = "") -> str:
        return f"{name}:{text}"
    return tool


if __name__ == "__main__":
    with open(sys.argv[1], encoding="utf-8") as f:
        spec = json.load(f)
    server.add_tool(env_names, name="env_names")
    for entry in spec.get("tools", []):
        server.add_tool(_make(entry["name"]), name=entry["name"], description=entry.get("description", ""))
    server.run()
