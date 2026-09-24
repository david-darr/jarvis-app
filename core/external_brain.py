"""Brain-interface-compatible wrapper around a registered "bring your own
model" endpoint (core/model_endpoints.py) — connect()/run_turn()/
run_turn_stream()/disconnect(), same shape as core/brain.py's Brain, so
services/chat_service.py can pick either implementation per session without
its own call sites caring which one they're talking to.

Unlike Brain (which delegates all conversation state to the Claude Agent
SDK's own connection), a plain OpenAI-compatible endpoint is stateless per
request — this class holds the running message list itself, seeded from the
session's already-persisted history on connect() so a pinned-model session
picks up mid-conversation correctly (e.g. after a server restart).

Shared memory + cross-session awareness (David's ask 2026-08-31, "out of
the box for all imported AI models both local and API") — this is the
non-Claude half. Claude already has native vault file access; a plain
OpenAI-compatible model has none at all today, so it gets search_vault,
read_vault_file, and search_sessions as real function-calling tools (see
core/providers/openai_compatible.py's tool-calling loop). Not every model
actually supports tool calling — that's a per-endpoint capability, degraded
gracefully in the provider client, not assumed here.

list_skills/read_skill added 2026-09-01 (David's ask: "all models... know,
utilize, and operate under jarvis's methods, skills, and memory, all as a
hive mind") — same reasoning, same shared engine (core/memory_tools.py).
"""
from typing import AsyncIterator

from core import integrations, mcp_client, permissions, projects, system_prompt, tool_registry
from core.providers import openai_compatible
from core.session_manager import sent_text

class ExternalBrain:
    def __init__(self, base_url: str, model: str, api_key: str | None, history: list[dict] | None = None,
                 session_id: str | None = None, num_ctx: int | None = None, is_admin: bool = False,
                 project_id: str | None = None, endpoint_id: str | None = None,
                 integration_ids: list[str] | None = None):
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self.session_id = session_id
        self.num_ctx = num_ctx
        # The shared hive-mind tools (core/tool_registry.py); run_shell only
        # for an admin, absent from a non-admin session's list entirely.
        self.is_admin = is_admin
        self.tools = tool_registry.openai_tools(is_admin)
        # MCP servers enabled for this chat (None: every registered one, as
        # for Claude), their tools found at connect() - see core/mcp_client.py.
        self.integration_ids = integration_ids
        self._mcp_tools: dict[str, dict] = {}
        # The "landing zone" (David's ask 2026-09-01, after live-testing
        # found chats couldn't answer real vault/memory questions) — a
        # real system message, not just tool descriptions, so a model
        # knows memory/skills exist and where to start looking, same
        # pointer-not-a-dump role core/brain.py's system_prompt plays for
        # Claude. Only prepended once, on a session with no prior history —
        # an existing conversation already carries its own system message
        # from when it was first created.
        seeded = self._seed(history or [], endpoint_id)
        if not seeded or seeded[0].get("role") != "system":
            # Projects (David's ask 2026-09-12) appended the same way as
            # core/brain.py/core/codex_brain.py — see core/projects.py's
            # project_addendum().
            seeded.insert(0, {"role": "system", "content": system_prompt.for_external(is_admin) + projects.project_addendum(project_id)})
        self._messages: list[dict] = seeded
        # Set on every completed turn that reported usage (David's ask
        # 2026-09-01, per-model token usage on Home) — best-effort, since
        # not every OpenAI-compatible endpoint returns it. Read by
        # services/chat_service.py right after run_turn()/run_turn_stream().
        self.last_usage: dict | None = None
        # This turn's tool calls and results, in the order sent. Filled while
        # the turn runs (so a stopped turn still has what ran); saved with the
        # reply by services/chat_service.py, which is how they survive a
        # reconnect. See _seed below.
        self.last_tool_rounds: list[dict] = []

    @staticmethod
    def _seed(history: list[dict], endpoint_id: str | None) -> list[dict]:
        """The saved transcript as this endpoint should see it, tool rounds
        included (prompt-cache audit finding 2, 2026-09-22). Each reply's
        rounds are saved on that reply and replayed just before it, which
        rebuilds exactly the request the live connection last sent - so a
        reconnect loses neither the earlier tool results nor the cached
        prefix.

        Only the endpoint that produced the rounds gets them back. Providers
        shape tool calls differently (Ollama sends arguments as an object,
        the OpenAI spec as a string), so one endpoint's rounds can be
        rejected by another; the reply text alone still carries over."""
        seeded = []
        for m in history:
            rounds = m.get("tool_rounds") or {}
            if endpoint_id and rounds.get("endpoint_id") == endpoint_id:
                seeded.extend(rounds.get("messages") or [])
            # What this endpoint was actually sent, attachment note and Open
            # Mic instruction included, so the rebuilt history still matches
            # the cached one (prompt-cache audit finding 4).
            seeded.append({"role": m["role"], "content": sent_text(m)})
        return seeded

    async def _execute_tool(self, name: str, args: dict) -> str:
        if name in self._mcp_tools:
            return await self._call_mcp(name, args)
        return await tool_registry.call(name, args, tool_registry.ToolContext(self.session_id, self.is_admin),
                                        tool_registry.OPENAI)

    async def _call_mcp(self, name: str, args: dict) -> str:
        """A third-party tool: asked about first, like Claude's MCP calls,
        through the chat's own permission prompt. With nobody to ask (no chat
        window open, a scheduled run) the broker refuses and says where to
        grant it."""
        spec = self._mcp_tools[name]
        decision = await permissions.decide(
            surface=f"chat:{self.session_id}" if self.session_id else "none",
            tool=name, arguments=args if isinstance(args, dict) else {},
            title=f"{spec['server']}: {spec['name']}", description=spec["description"][:300],
            is_admin=self.is_admin,
        )
        if decision.behavior != "allow":
            return f"Not run: {decision.reason}"
        try:
            return await mcp_client.call_tool(spec["config"], spec["name"], args)
        except Exception as e:
            return f"Tool error: {e}"

    async def connect(self) -> None:
        """Find the tools of the MCP servers this chat may use. Only for a
        real chat: a detached summariser gets none. The list is fixed for the
        connection, so the tool list - part of the cached prompt - is stable."""
        if not self.session_id:
            return
        servers = integrations.list_mcp_servers_runtime(self.integration_ids)
        if not servers:
            return
        self._mcp_tools = await mcp_client.discover(servers)
        self.tools = self.tools + [
            {"type": "function", "function": {"name": name, "parameters": spec["schema"],
                                              "description": f"[{spec['server']} MCP server] {spec['description']}".strip()}}
            for name, spec in self._mcp_tools.items()
        ]

    async def run_turn(self, user_text: str) -> str:
        self._messages.append({"role": "user", "content": user_text})
        self.last_tool_rounds = []
        reply = await openai_compatible.run_turn(
            self.base_url, self.model, self.api_key, self._messages,
            tools=self.tools, tool_executor=self._execute_tool,
            on_usage=lambda u: setattr(self, "last_usage", u), num_ctx=self.num_ctx,
            rounds=self.last_tool_rounds,
        )
        self._messages.extend(self.last_tool_rounds)
        self._messages.append({"role": "assistant", "content": reply})
        return reply

    async def run_turn_stream(self, user_text: str) -> AsyncIterator[str]:
        self._messages.append({"role": "user", "content": user_text})
        self.last_tool_rounds = []
        parts: list[str] = []
        async for chunk in openai_compatible.run_turn_stream(
            self.base_url, self.model, self.api_key, self._messages,
            tools=self.tools, tool_executor=self._execute_tool,
            on_usage=lambda u: setattr(self, "last_usage", u), num_ctx=self.num_ctx,
            rounds=self.last_tool_rounds,
        ):
            parts.append(chunk)
            yield chunk
        self._messages.extend(self.last_tool_rounds)
        self._messages.append({"role": "assistant", "content": "".join(parts)})

    async def disconnect(self) -> None:
        pass
