"""Channel-agnostic turn handler shared by every surface (chat UI, Discord, Telegram, etc).

Wraps the Claude Agent SDK so a caller only ever calls connect() / run_turn() /
run_turn_stream() / disconnect() and never touches the SDK directly. Ported
from jarvis-starter-kit's core/brain.py (v1) as the starting point for JARVIS
proper (v2) — extend here, not there; the starter kit stays a separate,
working v1 reference.

The agent's cwd is pointed at the vault directory (core/vault.py resolves and
seeds it) rather than a generic project dir — "memory lives in an Obsidian
vault, not in the model or in chat history" is the core reusable idea carried
over from v1, so the connected session's own file read/write tools operate on
vault notes by default. No separate memory-store API needed for the core
mechanism, same as how the real JARVIS (voice-line/brain.py) works today.
"""
import asyncio
import os

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
)

from core import hive_mind_server, integrations, settings as settings_store, system_prompt
from core.constants import REPO_CODE_DIRS
from core.vault import resolve_vault_dir

# David's ask 2026-09-03: run_turn_stream() used to await the Claude Code CLI
# subprocess with no timeout at all, so a hung/crashed subprocess (or a
# reconnect that never fully completes) left the chat UI spinning forever
# with no error. This caps the wait per received message, not per whole
# turn, so a legitimately long tool-call chain that's still making progress
# isn't cut off — only real silence trips it.
TURN_MESSAGE_TIMEOUT_SECONDS = 180


class Brain:
    """One Brain per conversation session. Create one, call connect() once, then
    run_turn(text)/run_turn_stream(text) per incoming message, and disconnect()
    on shutdown."""

    def __init__(self, vault_dir: str | None = None, cwd_override: str | None = None,
                 integration_ids: list[str] | None = None, session_id: str | None = None,
                 model: str | None = None, is_admin: bool = False):
        self.vault_dir = vault_dir or resolve_vault_dir()
        # Optional model override for a "Claude Code CLI" endpoint added in
        # Settings > Add Models (David's ask 2026-08-31 — Claude is no
        # longer a free default, it's a real addable connection with its
        # own optional model field). None keeps the `claude` CLI's own
        # default model, same as every session before this option existed.
        self.model = model
        # Set on every completed turn from the SDK's own ResultMessage.usage
        # (David's ask 2026-09-01: per-model token usage on Home) — read by
        # services/chat_service.py right after run_turn()/run_turn_stream()
        # finishes, since Brain itself doesn't know which model_endpoint_id
        # it's associated with.
        self.last_usage: dict | None = None
        # Only used to exclude this session's own history from cross-session
        # search results (David's ask 2026-08-31, shared memory + cross-
        # session awareness — see core/hive_mind_server.py) — a session
        # finding itself isn't "cross-session" anything.
        self.session_id = session_id
        # Workspace confinement (David's ask 2026-08-31, see core/workspace.py):
        # when a session is pinned to a folder, the agent's file/shell tools
        # should operate there instead of the vault. The Claude Agent SDK
        # scopes its own file/bash tools to `cwd`, so pointing cwd at the
        # workspace is the confinement mechanism itself — same mechanism the
        # vault-scoping already relies on, just a different target directory.
        self.cwd_override = cwd_override
        # Per-session integration/connector scoping (David's ask 2026-08-31,
        # matching Claude's own per-conversation connector toggle) — None
        # (the default) keeps every registered MCP server available, same
        # as the original global behavior.
        self.integration_ids = integration_ids
        # Shell execution (David's ask 2026-09-02, modeled on Odysseus's own
        # agent-tool gating — src/tool_security.py's owner_is_admin_or_
        # single_user()): the ONLY gate, no command blocklist, matching
        # their actual safety model. See _options() below.
        self.is_admin = is_admin
        self._client: ClaudeSDKClient | None = None

    def _options(self) -> ClaudeAgentOptions:
        # Settings > Admin > Agent Tools (David's ask 2026-08-31, matching
        # Odysseus's builtin-tool-toggle panel) — globally disabled tool
        # names, read fresh per connect() rather than cached at import time
        # so a Settings change takes effect on the next new session.
        disabled = settings_store.get_setting("disabled_tools") or []
        # Settings > Integrations > MCP Tool Server (David's ask 2026-08-31,
        # matching Odysseus's Integrations panel) — registered MCP servers
        # widen the agent's real tool access, read fresh per connect() same
        # as disabled_tools above, filtered to this session's chosen subset.
        mcp_servers = integrations.list_mcp_servers_runtime(self.integration_ids)
        # Shared memory + cross-session awareness (David's ask 2026-08-31):
        # Claude already has native file-tool access to the vault (its own
        # cwd below) — the only real gap is cross-session search, added
        # in-process (no subprocess/network hop) here.
        mcp_servers = {**mcp_servers, "hive_mind": hive_mind_server.get_hive_mind_server(self.session_id)}
        # Real gap found live: acceptEdits only auto-approves file-edit-type
        # prompts — a custom in-process MCP tool like search_sessions still
        # hit a permission prompt Claude has no way to answer headlessly, so
        # the tool silently never ran. Explicitly pre-approving it (not a
        # blanket bypassPermissions switch, which would also silently
        # auto-approve shell/bash) is the narrow fix. Every new hive_mind
        # tool needs the same pre-approval, same reason — checked the SDK's
        # own allowed_tools matcher (_whole_tool_allowed) directly: there's
        # no wildcard/prefix form that auto-approves a whole MCP server, so
        # each tool genuinely has to be listed by its exact full name here,
        # not a shortcut worth looking for again.
        # Shell execution (David's ask 2026-09-02) — admin-only, modeled on
        # Odysseus's own agent-tool gate (their src/tool_security.py). No
        # command blocklist, matching their actual safety model — the admin
        # check is the whole boundary. Claude already has a native Bash
        # tool; the only gap is that acceptEdits only auto-approves file
        # edits, so an un-pre-approved Bash call just hangs on a permission
        # prompt nobody can answer headlessly, same class of bug the
        # hive_mind tools below hit before they were pre-approved. A
        # non-admin session gets Bash explicitly denied (not just omitted)
        # so it fails closed immediately instead of hanging on that prompt.
        allowed_tools = [
            "mcp__hive_mind__search_sessions",
            "mcp__hive_mind__list_skills",
            "mcp__hive_mind__read_skill",
            "mcp__hive_mind__list_notes",
            "mcp__hive_mind__list_tasks",
            "mcp__hive_mind__list_upcoming_events",
            "mcp__hive_mind__list_specs",
            "mcp__hive_mind__read_spec",
            "mcp__hive_mind__list_documents",
            "mcp__hive_mind__read_document",
            "mcp__hive_mind__list_contacts",
            "mcp__hive_mind__list_task_runs",
            # Write tools (David's ask 2026-09-01) — go through the same
            # service layer the app's own routes use, see
            # services/notes_service.py.
            "mcp__hive_mind__create_note",
            "mcp__hive_mind__update_note",
            "mcp__hive_mind__delete_note",
            "mcp__hive_mind__create_task",
            "mcp__hive_mind__update_task",
            "mcp__hive_mind__delete_task",
            "mcp__hive_mind__create_event",
            "mcp__hive_mind__update_event",
            "mcp__hive_mind__delete_event",
        ]
        if self.is_admin:
            allowed_tools.append("Bash")
        else:
            disabled = [*disabled, "Bash"]

        return ClaudeAgentOptions(
            cwd=self.cwd_override or self.vault_dir,
            # Full read/write on jarvis-app's own source (David's ask
            # 2026-09-01) — cwd stays the vault (memory is still the core
            # model), this just additionally grants the app's own code so
            # Claude can do real dev work on jarvis-app itself. data/ is
            # deliberately not in this list — see REPO_CODE_DIRS' comment.
            add_dirs=REPO_CODE_DIRS,
            permission_mode="acceptEdits",
            disallowed_tools=disabled,
            mcp_servers=mcp_servers,
            allowed_tools=allowed_tools,
            model=self.model,
            # The "landing zone" (David's ask 2026-09-01, after live-testing
            # found chats couldn't answer real vault/memory questions) —
            # append to Claude Code's own default system prompt (a preset,
            # not a bare string override, so its existing tool-use
            # conventions aren't lost) rather than relying purely on tool
            # *descriptions* to imply a model should proactively check
            # memory.
            system_prompt={"type": "preset", "preset": "claude_code", "append": system_prompt.for_claude(self.is_admin)},
        )

    async def connect(self) -> None:
        self._client = ClaudeSDKClient(options=self._options())
        await self._client.connect()

    async def run_turn(self, user_text: str) -> str:
        """Non-streaming: waits for the full reply, returns it as one string."""
        parts = [chunk async for chunk in self.run_turn_stream(user_text)]
        return "".join(parts).strip()

    async def run_turn_stream(self, user_text: str):
        """Yields reply text incrementally as it arrives.

        Coarse-grained for this pass: yields once per completed TextBlock
        (potentially several per turn, e.g. across tool calls), not
        token-by-token. Real token-level streaming needs raw stream_event
        parsing (see voice-line's own brain.py::_handle_stream_event for the
        pattern) — worth doing once the UI actually wants that granularity;
        this is enough for a real incremental streaming experience for now.
        """
        if self._client is None:
            raise RuntimeError("Brain.connect() must be called before run_turn_stream().")

        await self._client.query(user_text)

        response_iter = self._client.receive_response().__aiter__()
        while True:
            try:
                message = await asyncio.wait_for(
                    response_iter.__anext__(), timeout=TURN_MESSAGE_TIMEOUT_SECONDS
                )
            except StopAsyncIteration:
                return
            except asyncio.TimeoutError:
                yield (
                    "\n\n[JARVIS: Claude Code didn't respond within "
                    f"{TURN_MESSAGE_TIMEOUT_SECONDS}s and may be stuck — try sending "
                    "your message again, or restart the app if it keeps happening.]"
                )
                return

            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        yield block.text
            if isinstance(message, ResultMessage):
                self.last_usage = message.usage
                break

    async def disconnect(self) -> None:
        if self._client is not None:
            await self._client.disconnect()
            self._client = None
