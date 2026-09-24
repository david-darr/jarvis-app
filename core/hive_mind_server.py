"""Claude's half of shared memory + cross-session awareness (David's ask
2026-08-31). Claude already has native file-tool access to the vault (its
cwd) — the only real gap for Claude is cross-session search, so this is
just that one tool, wired in-process (no subprocess/network hop — see
claude_agent_sdk.create_sdk_mcp_server, "better performance than external
MCP servers") rather than over stdio/http like the user-added integrations.

Skills tools added 2026-09-01 (David's ask: "all models... utilize and
operate under jarvis's methods, skills, and memory, all as a hive mind") —
a second real gap found the same way: Skills live in data/skills/, a
sibling of the vault directory, not inside it, so Claude's native file
tools (scoped to its vault cwd) genuinely can't see them either, same as
cross-session history before this file existed.

Notes/Tasks/Calendar/Specs tools added 2026-09-01, same day, after David
asked Claude about upcoming events/tasks and it said there weren't any —
same class of gap again: that's real app data under data/*.json, nowhere
near the vault cwd. Deliberately NOT raw file access to data/ itself (see
core/memory_tools.py's docstring — that directory also holds password
hashes, session tokens, and encrypted API keys); these go through the same
service layer the app's own routes use.

Documents/Contacts/task-run-history added 2026-09-01, same day, closing
the remaining gaps from an explicit item-by-item audit David asked for
("does the ai model know where to grab attachments, documents, sessions,
skills, vault, calendar_events.json, contacts.json...").

Since 2026-09-23 the tools themselves live in core/tool_registry.py, shared
with the OpenAI-compatible brain; this module only turns the registry's
Claude tools into an in-process MCP server.
"""
from claude_agent_sdk import create_sdk_mcp_server, tool

from core import tool_registry


def _handler(name: str, ctx: tool_registry.ToolContext):
    async def run(args: dict) -> dict:
        text = await tool_registry.call(name, args, ctx, tool_registry.CLAUDE)
        return {"content": [{"type": "text", "text": text}]}
    return run


def get_hive_mind_server(exclude_session_id: str | None = None, is_admin: bool = False):
    """exclude_session_id isn't threaded into the tool call itself (the SDK
    tool signature is fixed at server-creation time) — Brain passes its own
    session id in by building a fresh server per connection instead of one
    shared global instance, so a session never "finds" its own history."""
    ctx = tool_registry.ToolContext(session_id=exclude_session_id, is_admin=is_admin)
    tools = [tool(spec.name, spec.description, spec.schema)(_handler(spec.name, ctx))
             for spec in tool_registry.specs(tool_registry.CLAUDE)]
    return create_sdk_mcp_server(name="hive_mind", tools=tools)
