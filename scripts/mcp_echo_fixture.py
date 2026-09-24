"""A real MCP server over stdio for scripts/test_chat.py's MCP client tests.

One tool, `echo`, which answers "ECHO:<text>" and, when MCP_ECHO_LOG is set,
appends each call to that file - so a test can prove a refused call never
reached the server.
"""
import os

from mcp.server.mcpserver import MCPServer

server = MCPServer("echo")


@server.tool()
def echo(text: str) -> str:
    """Repeat the text back."""
    log = os.environ.get("MCP_ECHO_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    return f"ECHO:{text}"


if __name__ == "__main__":
    server.run()
