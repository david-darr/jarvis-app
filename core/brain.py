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
import hashlib
import json
import os

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    TextBlock,
)
from claude_agent_sdk.types import StreamEvent

from core import custom_tabs, hive_mind_server, image_gen, integrations, permissions, projects, settings as settings_store, system_prompt
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
                 model: str | None = None, is_admin: bool = False, project_id: str | None = None,
                 effort: str | None = None, resume_session_id: str | None = None):
        self.vault_dir = vault_dir or resolve_vault_dir()
        # Optional model override for a "Claude Code CLI" endpoint added in
        # Settings > Add Models (David's ask 2026-08-31 — Claude is no
        # longer a free default, it's a real addable connection with its
        # own optional model field). None keeps the `claude` CLI's own
        # default model, same as every session before this option existed.
        self.model = model
        # Reasoning effort for this session (David's ask 2026-09-15) — a
        # native field on ClaudeAgentOptions in the installed SDK, not a
        # wrapper of our own, so it's passed straight through below. None
        # means the field is never set at all, which is byte-for-byte the
        # behaviour every session had before this option existed; the
        # caller (routes/session_routes.py) validates the value against
        # core/model_catalog.py before it ever reaches here.
        self.effort = effort
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
        # Projects (David's ask 2026-09-12) — appended to the landing-zone
        # system prompt below, same mechanism regardless of which model a
        # given chat in the project is actually pinned to (see
        # core/projects.py's project_addendum()).
        self.project_id = project_id
        # Which surface a permission request should appear on. A session id is
        # the natural handle: the chat that asked is the chat that answers. A
        # turn with no session (a scheduled task, say) has no one watching, so
        # the broker denies rather than hanging - see core/permissions.py.
        self.surface = f"chat:{session_id}" if session_id else "none"
        # The Claude Code CLI session to reopen on connect, when this chat has
        # one (services/chat_service.py reads it from the session record).
        # Resuming keeps the real conversation - its turns, tool results and
        # attachment notes - and a prompt prefix the cache can reuse, where
        # a fresh connection would need the transcript replayed as one
        # message. Checked against the installed SDK (0.2.148+): `resume` is
        # a native ClaudeAgentOptions field, passed as --resume=<id>.
        self.resume_session_id = resume_session_id
        # The CLI session id this brain's turns ran in, from each
        # ResultMessage; chat_service persists it after a successful turn.
        self.cli_session_id: str | None = None
        # Digest of the settings-derived tools this connection was built with;
        # see tool_config_changed().
        self.tool_fingerprint: str | None = None
        self._client: ClaudeSDKClient | None = None

    def _tool_config(self) -> tuple[list[str], list[str], dict]:
        """The parts of a connection that come from global settings rather
        than from this chat: (disallowed tools, pre-approved tools, registered
        MCP servers). Read fresh each time, so tool_config_changed() can tell
        whether an open chat is running on settings that have since changed."""
        # Settings > Admin > Agent Tools (David's ask 2026-08-31, matching
        # Odysseus's builtin-tool-toggle panel) — globally disabled tool
        # names.
        disabled = settings_store.get_setting("disabled_tools") or []
        # Settings > Integrations > MCP Tool Server (David's ask 2026-08-31,
        # matching Odysseus's Integrations panel) — registered MCP servers
        # widen the agent's real tool access, filtered to this session's
        # chosen subset.
        mcp_servers = integrations.list_mcp_servers_runtime(self.integration_ids)
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
        # Shell execution is admin-only. David restored the original automatic
        # admin shell behavior after command-by-command prompts became noisy;
        # its built-in grants remain visible and revocable in Permissions.
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
            "mcp__hive_mind__save_generated_image",
            "mcp__hive_mind__save_generated_file",
            # Canva image/design generation (David's ask 2026-09-10, "have
            # their claude code use Canva"). Pre-approves only the
            # generate-and-export surface actually needed for "create an
            # image" - not Canva's full tool set (nothing that edits/deletes
            # existing designs, comments, brand templates, etc.). This is
            # the account's own Canva connector, entirely outside jarvis-app's
            # control - if it isn't connected, these calls fail naturally and
            # Claude reports that; nothing here can detect or force it.
            # Both the current live tool (generate-design, deprecated but
            # still what this account's connector actually serves) and its
            # documented replacement (create-design) are pre-approved so
            # this doesn't silently break whenever Canva finishes that
            # rollout.
            "mcp__claude_ai_Canva__generate-design",
            "mcp__claude_ai_Canva__create-design",
            "mcp__claude_ai_Canva__get-design-candidates",
            "mcp__claude_ai_Canva__create-design-from-candidate",
            "mcp__claude_ai_Canva__get-export-formats",
            "mcp__claude_ai_Canva__export-design",
            "mcp__claude_ai_Canva__list-brand-kits",
            "mcp__claude_ai_Canva__get-assets",
        ]
        # Escape hatch for whatever this hardcoded baseline doesn't cover —
        # see core/settings.py's extra_allowed_tools for why this exists.
        allowed_tools.extend(settings_store.get_setting("extra_allowed_tools") or [])
        if self.is_admin:
            allowed_tools.extend(("Bash", "PowerShell"))
        else:
            disabled = [*disabled, "Bash", "PowerShell"]
        # App-wide approval (David's ask 2026-09-18). The pre-approved list
        # above is written into the permission store as visible, revocable
        # rules. Anything outside it used to hang on a prompt nothing could
        # answer; it now reaches the person instead. Admin shell grants are
        # seeded here too, so Settings can show and revoke the auto behavior.
        permissions.ensure_seeded(allowed_tools)
        # Only what still has a standing grant is pre-approved. Revoking a
        # built-in in Settings > Permissions used to change nothing: this list
        # was passed whole, and the SDK never consults can_use_tool for a tool
        # on it (found 2026-09-22). A revoked tool is now asked about, and
        # because this list feeds the fingerprint, an open chat picks the
        # revocation up on its next message.
        grants = permissions.standing_grants()
        allowed_tools = [tool for tool in allowed_tools if tool in grants and tool not in disabled]
        return disabled, allowed_tools, mcp_servers

    @staticmethod
    def _fingerprint(disabled: list[str], allowed_tools: list[str], mcp_servers: dict) -> str:
        # A digest, never the config itself: MCP entries carry decrypted keys.
        blob = json.dumps([disabled, allowed_tools, mcp_servers], sort_keys=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def tool_config_changed(self) -> bool:
        """Whether global settings have changed this connection's tools since
        it connected (prompt-cache audit finding 3, 2026-09-22).

        These settings used to land in an open chat only at its next
        reconnect - a Stop, an error, a restart - so a tool disabled in
        Settings stayed usable in every open chat until then, and the change
        then broke the cached prompt at an arbitrary moment. services/
        chat_service.py asks this before each turn and reconnects (resuming
        the same CLI session) when it is true, so a change lands on each
        chat's next message instead. Not deferred to new chats, as Hermes
        does by default: JARVIS chats live for weeks, and these settings are
        mostly safety switches."""
        return self._client is not None and self._fingerprint(*self._tool_config()) != self.tool_fingerprint

    def _options(self) -> ClaudeAgentOptions:
        disabled, allowed_tools, mcp_servers = self._tool_config()
        self.tool_fingerprint = self._fingerprint(disabled, allowed_tools, mcp_servers)
        # Shared memory + cross-session awareness (David's ask 2026-08-31):
        # Claude already has native file-tool access to the vault (its own
        # cwd below) — the only real gap is cross-session search, added
        # in-process (no subprocess/network hop) here.
        mcp_servers = {**mcp_servers, "hive_mind": hive_mind_server.get_hive_mind_server(self.session_id)}

        # A generated file needs somewhere to be built that isn't the vault
        # (David's ask 2026-09-12, after a live test found Claude writing a
        # requested .pptx straight into vault root — see
        # system_prompt.py's _GENERATED_FILES_ADDENDUM) — must exist before
        # the SDK will grant access to it.
        os.makedirs(image_gen.GENERATED_FILES_DIR, exist_ok=True)

        return ClaudeAgentOptions(
            can_use_tool=self._permission,
            cwd=self.cwd_override or self.vault_dir,
            # Full read/write on jarvis-app's own source (David's ask
            # 2026-09-01) — cwd stays the vault (memory is still the core
            # model), this just additionally grants the app's own code so
            # Claude can do real dev work on jarvis-app itself. data/ is
            # deliberately not in this list — see REPO_CODE_DIRS' comment.
            # generated_files IS included (unlike the rest of data/) so a
            # requested deliverable can be built there directly instead of
            # in the vault. User-built tab source is the one narrow data/
            # exception: generated tabs live there and need file-tool access.
            add_dirs=REPO_CODE_DIRS + [image_gen.GENERATED_FILES_DIR, custom_tabs.USER_TABS_DIR],
            # Found live 2026-09-08: the SDK's own default here is 1MB, and
            # a handful of real photos read back through a file tool (see
            # core/attachments.py) easily produces one JSON message from the
            # CLI bigger than that, which is a fatal transport error, not a
            # per-turn one — see services/chat_service.py's brain-eviction
            # comment for what that cascaded into. 10MB comfortably covers
            # several normal images with real headroom left; not raised
            # further than that so one truly runaway response still hits a
            # real ceiling instead of growing memory unbounded.
            max_buffer_size=10 * 1024 * 1024,
            permission_mode="acceptEdits",
            disallowed_tools=disabled,
            mcp_servers=mcp_servers,
            allowed_tools=allowed_tools,
            model=self.model,
            effort=self.effort,
            resume=self.resume_session_id,
            include_partial_messages=True,
            # The "landing zone" (David's ask 2026-09-01, after live-testing
            # found chats couldn't answer real vault/memory questions) —
            # append to Claude Code's own default system prompt (a preset,
            # not a bare string override, so its existing tool-use
            # conventions aren't lost) rather than relying purely on tool
            # *descriptions* to imply a model should proactively check
            # memory.
            system_prompt={"type": "preset", "preset": "claude_code", "append": system_prompt.for_claude(self.is_admin) + projects.project_addendum(self.project_id)},
        )

    async def _permission(self, tool_name: str, arguments: dict, context):
        """Ask the person, using the SDK's own idea of what a grant covers.

        `context.suggestions` is what Claude Code shows as "always allow this
        command" - the SDK proposes the rule, so the scope offered here is the
        one the model's own permission system would apply rather than a
        guess made in this app. Falling back to a derived scope keeps the
        prompt useful when no suggestion arrives.
        """
        rule_content = None
        for suggestion in getattr(context, "suggestions", None) or []:
            for rule in getattr(suggestion, "rules", None) or []:
                if getattr(rule, "tool_name", None) == tool_name and getattr(rule, "rule_content", None):
                    rule_content = rule.rule_content
                    break
            if rule_content:
                break
        decision = await permissions.decide(
            surface=self.surface,
            tool=tool_name,
            arguments=arguments if isinstance(arguments, dict) else {},
            is_admin=self.is_admin,
            target=rule_content or permissions.derive_target(tool_name, arguments),
            title=getattr(context, "title", None) or getattr(context, "display_name", None) or tool_name,
            description=getattr(context, "description", None) or getattr(context, "decision_reason", None) or "",
        )
        if decision.behavior == "allow":
            return PermissionResultAllow()
        # The reason goes into the transcript, so the model is told it was
        # refused and why rather than silently failing or trying again.
        return PermissionResultDeny(message=decision.reason, interrupt=False)

    async def connect(self) -> None:
        self._client = ClaudeSDKClient(options=self._options())
        await self._client.connect()

    async def run_turn(self, user_text: str) -> str:
        """Non-streaming: waits for the full reply, returns it as one string."""
        parts = [chunk async for chunk in self.run_turn_stream(user_text)]
        return "".join(parts).strip()

    async def run_turn_stream(self, user_text: str):
        """Stream visible text deltas, never reasoning or tool-input events.
        Completed blocks remain a fallback for CLI versions without deltas.
        """
        if self._client is None:
            raise RuntimeError("Brain.connect() must be called before run_turn_stream().")

        await self._client.query(user_text)

        response_iter = self._client.receive_response().__aiter__()
        streamed_blocks = {}
        has_text = False
        while True:
            try:
                message = await asyncio.wait_for(
                    response_iter.__anext__(), timeout=TURN_MESSAGE_TIMEOUT_SECONDS
                )
            except StopAsyncIteration:
                raise RuntimeError("Claude Code ended before reporting completion")
            except asyncio.TimeoutError:
                raise RuntimeError("Claude Code timed out waiting for a response")

            if isinstance(message, StreamEvent):
                if message.parent_tool_use_id:
                    continue
                event = message.event
                if event.get("type") == "message_start":
                    streamed_blocks = {}
                elif event.get("type") == "content_block_delta" and event.get("delta", {}).get("type") == "text_delta":
                    chunk = event["delta"].get("text", "")
                    index = event.get("index", 0)
                    if chunk:
                        if index not in streamed_blocks and has_text:
                            yield "\n\n"
                        streamed_blocks[index] = streamed_blocks.get(index, "") + chunk
                        has_text = True
                        yield chunk
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        if block.text in streamed_blocks.values():
                            continue
                        if has_text:
                            yield "\n\n"
                        has_text = True
                        yield block.text
            if isinstance(message, ResultMessage):
                self.last_usage = message.usage
                self.cli_session_id = message.session_id or self.cli_session_id
                if message.is_error:
                    raise RuntimeError("Claude Code reported an unsuccessful turn")
                break

    async def disconnect(self) -> None:
        if self._client is not None:
            await self._client.disconnect()
            self._client = None
