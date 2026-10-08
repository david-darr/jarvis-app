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
import asyncio
import contextlib
from typing import AsyncIterator

from core import attachments, integrations, mcp_client, mcp_oauth, permissions, projects, runs, system_prompt, tool_registry, tool_search
from core.providers import openai_compatible
from core.session_manager import sent_text
from core.turn_taint import TurnTaint

class ExternalBrain:
    def __init__(self, base_url: str, model: str, api_key: str | None, history: list[dict] | None = None,
                 session_id: str | None = None, num_ctx: int | None = None, is_admin: bool = False,
                 project_id: str | None = None, endpoint_id: str | None = None,
                 integration_ids: list[str] | None = None, allow_user_tab_source: bool = False,
                 supports_images: bool = False, agent_id: str | None = None, agent_prompt: str = "",
                 window: int | None = None, helper: bool = False):
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self.session_id = session_id
        # The agent this brain works for, if any (services/agent_service.py).
        self.agent_id = agent_id
        self.num_ctx = num_ctx
        # The shared hive-mind tools (core/tool_registry.py); run_shell only
        # for an admin, absent from a non-admin session's list entirely.
        self.is_admin = is_admin
        self.allow_user_tab_source = allow_user_tab_source
        self.supports_images = supports_images
        self.turn_taint = TurnTaint()
        self.pending_reference_taint = False
        # A known small window (local models, small API ones) gets the core
        # tools and the bridge; the rest are searched for (2026-10-06,
        # core/tool_registry.py). Decided once, so the cached prompt is stable.
        # A helper (core/helpers.py) gets the read tools and browse only, and
        # its own short instructions instead of a chat's. That list is short
        # enough to show whole on any window, so no tool is hidden from it.
        self.helper = helper
        self.small_window = bool(window and window <= tool_registry.SMALL_WINDOW) and not helper
        self.tools = tool_registry.openai_tools(is_admin, agent=bool(agent_id), small_window=self.small_window,
                                                helper=helper)
        self._deferred: dict[str, dict] = tool_registry.deferred_tools(is_admin, agent=bool(agent_id), helper=helper) \
            if self.small_window else {}
        if self._deferred:
            self.tools = self.tools + tool_search.bridge_schemas()
        if is_admin:
            # A visible, revocable built-in grant, like Claude's Bash; see
            # the run_shell tool in core/tool_registry.py.
            permissions.ensure_seeded(["run_shell"])
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
        seeded = self._seed(history or [], endpoint_id, supports_images)
        if helper:
            seeded.insert(0, {"role": "system", "content": system_prompt.HELPER_PROMPT})
        elif not seeded or seeded[0].get("role") != "system":
            # Projects (David's ask 2026-09-12) appended the same way as
            # core/brain.py/core/codex_brain.py — see core/projects.py's
            # project_addendum().
            seeded.insert(0, {"role": "system", "content": system_prompt.for_external(is_admin, allow_user_tab_source)
                             + (system_prompt.DEFERRED_TOOLS_ADDENDUM if self._deferred else "")
                             + projects.project_addendum(project_id)
                             + (f"\n\n{agent_prompt}" if agent_prompt else "")})
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
        # The tool that was running when this turn was stopped, if one was.
        self.interrupted_tool: str | None = None

    @staticmethod
    def _seed(history: list[dict], endpoint_id: str | None, supports_images: bool = False) -> list[dict]:
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
            content = sent_text(m)
            if supports_images and m["role"] == "user" and m.get("image_attachment_ids"):
                content = attachments.openai_image_content(content, m["image_attachment_ids"])
            seeded.append({"role": m["role"], "content": content})
        return seeded

    async def _execute_tool(self, name: str, args: dict) -> str:
        """One tool call, with the person's lifecycle hooks around it
        (services/hook_service.py): a before-tool hook can block it. The
        tool search and describe steps are lookups, not tools, so they pass."""
        args = args if isinstance(args, dict) else {}
        if name in (tool_search.SEARCH, tool_search.DESCRIBE):
            return await self._run_tool(name, args)
        from services.hook_service import hook_service
        real = args.get("name", "") if name == tool_search.CALL else name
        real_args = args.get("arguments") if name == tool_search.CALL else args

        async def run():
            try:
                return await self._run_tool(name, args)
            except asyncio.CancelledError:
                # The turn was stopped while the tool ran; see cancel().
                self.interrupted_tool = real or name
                raise
        return await hook_service.around_tool(real, real_args, run, **self._context().hook_context())

    def _catalog(self) -> dict[str, dict]:
        """What the bridge searches: JARVIS's deferred tools, then this
        chat's MCP tools."""
        return {**self._deferred, **self._mcp_tools}

    async def _run_tool(self, name: str, args: dict) -> str:
        if name == tool_search.SEARCH:
            result = tool_search.search(self._catalog(), args.get("query", ""), args.get("limit", 5))
            # Third-party descriptions are untrusted text; JARVIS's own are not.
            if result.startswith("[") and tool_search.search(self._mcp_tools, args.get("query", ""), 10).startswith("["):
                self.turn_taint.mark("MCP tool catalog")
            return result
        if name == tool_search.DESCRIBE:
            selected = args.get("name", "")
            result = tool_search.describe(self._catalog(), selected)
            if isinstance(selected, str) and selected in self._mcp_tools:
                self.turn_taint.mark("MCP tool catalog")
            return result
        if name == tool_search.CALL:
            selected = args.get("name", "")
            if not isinstance(selected, str) or selected not in self._catalog():
                return "Tool not available in this chat. Search again for an enabled tool."
            if not isinstance(args.get("arguments"), dict):
                return "Tool arguments must be an object. Call jarvis_tool_describe to see its schema."
            if selected in self._mcp_tools:
                return await self._call_mcp(selected, args["arguments"])
            return await tool_registry.call(selected, args["arguments"], self._context(), tool_registry.OPENAI)
        if name in self._mcp_tools:
            return await self._call_mcp(name, args)
        return await tool_registry.call(name, args, self._context(), tool_registry.OPENAI)

    def _context(self) -> tool_registry.ToolContext:
        return tool_registry.ToolContext(self.session_id, self.is_admin, self.allow_user_tab_source,
                                         self.turn_taint, self.agent_id, model=self.model, helper=self.helper)

    async def _call_mcp(self, name: str, args: dict) -> str:
        """A third-party tool: asked about first, like Claude's MCP calls,
        through the chat's own permission prompt. With nobody to ask (no chat
        window open, a scheduled run) the broker refuses and says where to
        grant it."""
        spec = self._mcp_tools[name]
        current = integrations.list_mcp_servers_runtime(self.integration_ids).get(spec["server"])
        if not self._same_mcp_endpoint(spec["config"], current):
            return "Not run: this integration is no longer enabled for the chat."
        decision = await permissions.decide(
            surface=self._context().permission_surface,
            tool=name, arguments=args if isinstance(args, dict) else {},
            title=f"{spec['server']}: {spec['name']}", description=spec["description"][:300],
            is_admin=self.is_admin,
        )
        if decision.behavior != "allow":
            return f"Not run: {decision.reason}"
        # A signed-in server's token can expire during a long chat; the
        # config is re-read so each call carries the current one.
        await mcp_oauth.refresh_due(self.integration_ids)
        config = integrations.list_mcp_servers_runtime(self.integration_ids).get(spec["server"])
        if not self._same_mcp_endpoint(spec["config"], config):
            return "Not run: this integration is no longer enabled for the chat."
        try:
            result = await mcp_client.call_tool(config, spec["name"], args)
            self.turn_taint.mark(f"MCP result: {name}")
            return result
        except Exception as e:
            return f"Tool error: {e}"

    @staticmethod
    def _same_mcp_endpoint(discovered: dict, current: dict | None) -> bool:
        """Token refresh is fine; a replaced server with the same name is not."""
        return current is not None and all(
            discovered.get(key) == current.get(key) for key in ("type", "url", "command", "args")
        )

    async def connect(self) -> None:
        """Find the tools of the MCP servers this chat may use. Only for a
        real chat: a detached summariser gets none. The list is fixed for the
        connection, so the tool list - part of the cached prompt - is stable."""
        if not self.session_id and not self.agent_id:
            return
        await mcp_oauth.refresh_due(self.integration_ids)
        servers = integrations.list_mcp_servers_runtime(self.integration_ids)
        if not servers:
            return
        self._mcp_tools = await mcp_client.discover(servers)
        if self._mcp_tools and not self._deferred:
            self.tools = self.tools + tool_search.bridge_schemas()

    async def run_turn(self, user_text: str | list[dict]) -> str:
        self.turn_taint.reset()
        if self.pending_reference_taint:
            self.turn_taint.mark(self.pending_reference_taint if isinstance(self.pending_reference_taint, str)
                                 else "selected reference")
            self.pending_reference_taint = False
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

    def run_turn_stream(self, user_text: str | list[dict]) -> AsyncIterator[str]:
        """The reply's text: the text of events()."""
        return runs.text_only(self.events(user_text))

    async def events(self, user_text: str | list[dict], stream: bool = True) -> AsyncIterator[runs.RunEvent]:
        """The turn as typed events (core/runs.py), from the provider loop:
        text, each tool call started and finished, each provider call's
        usage, then RESULT. The history is brought up to date before RESULT
        goes out, so it is current whatever the caller does after it.
        stream=False: plain requests, as run_turn makes (see turn_events)."""
        self.turn_taint.reset()
        if self.pending_reference_taint:
            self.turn_taint.mark(self.pending_reference_taint if isinstance(self.pending_reference_taint, str)
                                 else "selected reference")
            self.pending_reference_taint = False
        self._messages.append({"role": "user", "content": user_text})
        self.last_tool_rounds = []
        self.interrupted_tool = None
        parts: list[str] = []
        async with contextlib.aclosing(openai_compatible.turn_events(
            self.base_url, self.model, self.api_key, self._messages,
            tools=self.tools, tool_executor=self._execute_tool,
            on_usage=lambda u: setattr(self, "last_usage", u), num_ctx=self.num_ctx,
            rounds=self.last_tool_rounds, stream=stream,
        )) as items:
            async for item in items:
                if item.kind is runs.EventKind.TEXT:
                    parts.append(item.data["text"])
                elif item.kind is runs.EventKind.RESULT:
                    self._messages.extend(self.last_tool_rounds)
                    self._messages.append({"role": "assistant", "content": "".join(parts)})
                yield item

    async def cancel(self) -> runs.StopResult:
        """Nothing of this brain runs on its own once its turn is closed, so
        a stop is confirmed unless a tool was cut off mid-run: a command or
        sandbox run started by a tool may still finish."""
        if self.interrupted_tool:
            return runs.StopResult(False, f"the tool {self.interrupted_tool} was running when the turn stopped and may still finish")
        return runs.StopResult(True, "the request was closed; the provider may still bill a call it had started")

    async def disconnect(self) -> None:
        pass
