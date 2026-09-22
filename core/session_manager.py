"""Chat session persistence — multiple named sessions, each with its own
message history, matching the "multiple open chats you can enter into or
search for/create" requirement from JARVIS Plan's tab-content scoping.

This module is the facade every other part of the app talks to; the storage
lives in its sibling core/session_manager_store.py, which holds the SQLite
schema, the FTS5 search index and the one-time JSON import. Nothing outside
these two files should know how a session is stored.

Storage moved from one-JSON-file-per-session to SQLite on 2026-09-22. The
short version of why: the old layout wrote the session body and a separate
`sessions_index.json` as two independent writes, and every read path gated
on the index, so the two could disagree and strand a chat that was sitting
right there on disk. The sibling's docstring has the full account. Public
method signatures here did not change as part of that move.
"""
import time
import uuid
from typing import Optional

from core import session_manager_store as store


class SessionManager:
    # No __init__: the store opens lazily on first use and runs the legacy
    # JSON import itself, so the migration doesn't depend on this class
    # having been constructed (or on which module imported what first).

    def create_session(self, title: str = "New Chat") -> dict:
        session_id = uuid.uuid4().hex[:12]
        now = time.time()
        session = {
            "id": session_id,
            "title": title,
            "starred": False,
            "created_at": now,
            "updated_at": now,
            "messages": [],
            "model_endpoint_id": None,  # None = no model chosen yet (David's ask 2026-08-31: no default model — see services/chat_service.py's NO_MODEL_MESSAGE)
            "workspace_dir": None,  # None = agent's tools stay scoped to the vault
            # Codex CLI's own server-side thread id (see core/codex_brain.py) —
            # None until this session's first turn through a codex_cli
            # endpoint. Unlike the Claude Agent SDK, `codex exec resume
            # <thread_id>` gives real cross-restart conversation continuity,
            # so this is persisted here rather than only held in memory.
            "codex_thread_id": None,
            # Projects (David's ask 2026-09-12) — None = not in a project.
            # See core/projects.py's project_addendum(): every brain kind
            # appends the assigned project's instructions/documents to its
            # landing-zone prompt, so a chat keeps this regardless of which
            # model it's pinned to.
            "project_id": None,
            # Which Settings > Integrations MCP Tool Servers this chat can
            # reference (David's ask 2026-08-31, matching Claude's per-
            # conversation connector toggle) — None = all registered ones
            # (matches pre-existing global behavior); a list restricts to
            # just those ids.
            "enabled_integration_ids": None,
        }
        store.save_session(session)
        return session

    def list_sessions(self) -> list[dict]:
        """Starred first, then newest-first within each group — metadata only
        (no message bodies). Right-click star/delete is David's ask, 2026-08-31.

        The ordering is the store's index rather than a Python sort, and the
        counts are derived from the stored messages on every write, so a
        listing can no longer describe a session differently from its body.
        """
        return store.list_sessions()

    def set_starred(self, session_id: str, starred: bool) -> dict:
        session = self._require(session_id)
        session["starred"] = starred
        return store.save_session(session)

    def get_session(self, session_id: str) -> Optional[dict]:
        return store.get_session(session_id)

    def _require(self, session_id: str) -> dict:
        """Load a session or raise the KeyError every caller already expects.

        The old code checked index membership first and then loaded, which
        meant a session present in one and missing from the other produced
        either a KeyError or a None depending on which method you called.
        One lookup, one outcome.
        """
        session = store.get_session(session_id)
        if session is None:
            raise KeyError(f"no such session: {session_id}")
        return session

    def append_message(self, session_id: str, role: str, content: str, status: str = "complete") -> None:
        session = self._require(session_id)
        session["messages"].append({"role": role, "content": content, "ts": time.time(), "status": status})
        session["updated_at"] = time.time()

        # Auto-title from the first user message, same idea as most chat UIs
        # (Odysseus included) — a session named "New Chat" forever isn't
        # findable in a sidebar list.
        if session["title"] == "New Chat" and role == "user":
            session["title"] = content[:60]

        store.save_session(session)

    def set_model_endpoint(self, session_id: str, model_endpoint_id: Optional[str], model_override: Optional[str] = None,
                           model_effort: Optional[str] = None) -> dict:
        """Pin an endpoint and optional CLI model; None means no model chosen.
        model_override: None inherits the endpoint, empty string uses the CLI
        default, a nonempty string selects an exact model for this chat only.
        model_effort: None sends no reasoning effort at all (the behaviour
        every session had before the option existed); otherwise a value the
        caller has already validated against core/model_catalog.py.

        An effort change deliberately does NOT clear codex_thread_id, unlike
        an endpoint change below. Verified live before deciding that:
        `-c model_reasoning_effort` is accepted ahead of codex's `resume`
        subcommand, so a resumed thread genuinely picks up the new effort on
        its next turn — throwing the thread away would discard real
        conversation history to no purpose. The caller validates kind and
        holds the session operation guard."""
        session = self._require(session_id)
        # Read both before either is overwritten — comparing after assignment
        # would make these checks dead code.
        endpoint_changed = session.get("model_endpoint_id") != model_endpoint_id
        model_changed = endpoint_changed or session.get("model_override") != model_override
        if endpoint_changed:
            # A different endpoint must not resume an old provider's thread.
            # Saved messages are replayed into a fresh Codex thread instead.
            session["codex_thread_id"] = None
        if model_changed:
            # A different model has a different context capacity, so the
            # stored occupancy no longer describes anything real. Cleared
            # rather than left to render a stale percentage against the new
            # model's window until the next turn overwrites it.
            session["context_state"] = None
        session["model_endpoint_id"] = model_endpoint_id
        session["model_override"] = model_override
        session["model_effort"] = model_effort
        return store.save_session(session)

    def set_context_state(self, session_id: str, state: Optional[dict]) -> None:
        """Records this chat's CURRENT context occupancy (David's ask
        2026-09-15) — overwritten every turn, never accumulated. That
        overwrite is the whole mechanism: after a compaction the next turn
        simply reports a smaller number and the meter falls, with no
        compaction event to detect or subscribe to. Storing it on the
        session record (rather than a process-level dict) is what makes it
        survive a reload and stay correct when switching between chats —
        each session carries its own, and a deleted session takes its
        reading with it.

        Best-effort by the same convention as record_usage(): a turn whose
        provider reported no usable usage passes None and simply leaves the
        previous reading in place rather than blanking a good value. Never
        raises — a bookkeeping failure must not fail a turn that already
        succeeded."""
        if state is None:
            return
        session = store.get_session(session_id)
        if session is None:
            return
        session["context_state"] = state
        store.save_session(session)

    def set_codex_thread_id(self, session_id: str, thread_id: Optional[str]) -> dict:
        """Records the Codex CLI thread id a session's first codex_cli turn
        created, so every later turn (including after a server restart)
        resumes the same server-side thread instead of starting a blank one.
        Not surfaced in the listing — internal bookkeeping only, not
        something the UI lists sessions by."""
        session = self._require(session_id)
        session["codex_thread_id"] = thread_id
        return store.save_session(session)

    def register_artifact(self, session_id: str, url: str) -> None:
        """Make a published file previewable before the turn finishes."""
        session = self._require(session_id)
        urls = session.setdefault("artifact_urls", [])
        if url not in urls:
            urls.append(url)
            store.save_session(session)

    def set_open_mic(self, session_id: str, active: bool) -> dict:
        """Marks a session as an Open Mic conversation (David's ask
        2026-09-15).

        Stored on the session rather than held in the page so the mode
        survives a reload and is visible to any surface listing sessions. The
        turns themselves are ordinary messages - nothing about a voice turn is
        stored differently - so leaving the mode leaves a real conversation
        behind rather than a separate voice log needing to be merged.

        `open_mic_started_at` marks where the spoken stretch began, which is
        what summarise_open_mic() uses to know how far back to reach. It is
        cleared on exit so a later stretch cannot accidentally re-summarise an
        earlier one.
        """
        session = self._require(session_id)
        session["open_mic"] = bool(active)
        if active:
            session.setdefault("open_mic_started_at", len(session.get("messages", [])))
        else:
            session.pop("open_mic_started_at", None)
        return store.save_session(session)

    def effective_messages(self, session_id: Optional[str], exclude_last: bool = False) -> list[dict]:
        """What a brain should treat as this session's prior conversation:
        the full stored transcript, or — once compact_session() has run —
        the latest compaction's summary followed by the messages after its
        boundary. Every one of chat_service.py's/core/codex_brain.py's three
        history-injection points calls this instead of reading
        session["messages"] directly, so "compact this chat" only has to be
        taught here once.

        Never a destructive view: the messages before the boundary are still
        stored (just flagged archived by compact_session), this only decides
        what gets sent to the model.

        exclude_last drops the most-recently-appended message — the current
        turn's own user message, already saved by the time a brain asks for
        "prior" history — matching the "[:-1]" convention both call sites
        used before this existed.
        """
        session = self.get_session(session_id) if session_id else None
        if session is None:
            return []
        messages = session.get("messages", [])
        compactions = session.get("compactions") or []
        if compactions:
            latest = compactions[-1]
            summary_note = {
                "role": "assistant",
                "content": (
                    "[Earlier parts of this conversation were compacted to save context space. "
                    "Summary of what happened before this point:]\n\n" + latest["summary"]
                ),
            }
            result = [summary_note] + messages[latest["through_index"]:]
        else:
            result = list(messages)
        if exclude_last and result:
            result = result[:-1]
        return result

    def compact_session(self, session_id: str, through_index: int, summary: str) -> dict:
        """Records a compaction checkpoint without deleting or rewriting any
        stored message. Messages before through_index get an in-place
        "archived" flag (still stored, still visible in the transcript,
        still exported/searched) purely as a record of what a compaction
        folded in — effective_messages() above is what actually changes
        future turns' behavior, using through_index/summary, not this flag.
        Appends to session["compactions"] rather than overwriting it, so a
        chat can be compacted more than once over its life; only the latest
        entry is ever read back."""
        session = self._require(session_id)
        messages = session.get("messages", [])
        through_index = max(0, min(through_index, len(messages)))
        for m in messages[:through_index]:
            m["archived"] = True
        compactions = session.setdefault("compactions", [])
        compactions.append({"through_index": through_index, "summary": summary, "created_at": time.time()})
        session["updated_at"] = time.time()
        return store.save_session(session)

    def replace_messages(self, session_id: str, start_index: int, replacement: list) -> dict:
        """Swaps a run of messages for a shorter stand-in, keeping everything
        before it untouched.

        Used to fold a spoken stretch into a summary when Open Mic ends. The
        slice is replaced rather than appended to, because the point is that
        the long back-and-forth stops occupying the context while its substance
        is kept.
        """
        session = self._require(session_id)
        messages = session.get("messages", [])
        start = max(0, min(start_index, len(messages)))
        session["messages"] = messages[:start] + replacement
        session["updated_at"] = time.time()
        return store.save_session(session)

    def set_project(self, session_id: str, project_id: Optional[str]) -> dict:
        """Assigns a session to a project (core/projects.py), or clears it
        back to None. Caller (routes/session_routes.py) is responsible for
        closing any live brain afterward — same pattern as set_model_endpoint/
        set_workspace, since the project's instructions/documents are only
        injected at connection time."""
        session = self._require(session_id)
        session["project_id"] = project_id
        return store.save_session(session)

    def set_workspace(self, session_id: str, workspace_dir: Optional[str]) -> dict:
        """Pin a session's agent tools to a specific folder (see
        core/workspace.py's vet_workspace — the caller must vet before
        calling this), or clear back to None for the default vault scope."""
        session = self._require(session_id)
        session["workspace_dir"] = workspace_dir
        return store.save_session(session)

    def set_integrations(self, session_id: str, enabled_integration_ids: Optional[list[str]]) -> dict:
        """Restrict which MCP Tool Server integrations this chat can
        reference (David's ask 2026-08-31), or clear back to None for "all
        registered ones" — same distinction Claude's own per-conversation
        connector toggle makes."""
        session = self._require(session_id)
        session["enabled_integration_ids"] = enabled_integration_ids
        return store.save_session(session)

    def rename_session(self, session_id: str, title: str) -> None:
        session = self._require(session_id)
        session["title"] = title
        store.save_session(session)

    def delete_session(self, session_id: str) -> None:
        store.delete_session(session_id)

    def get_channel_session_id(self, channel_key: str) -> Optional[str]:
        """Look up a channel's already-pinned session without creating one
        (David's ask 2026-09-01, real gap found live: changing a Discord
        bot's default model in Settings only ever affected a session
        created *after* that point — an already-existing channel
        conversation, exactly what David actually hit, kept whatever model
        it started with, silently ignoring the new Settings value). Used by
        routes/settings_routes.py to also push a model change onto an
        existing session, not just future ones.

        A mapping pointing at a session that no longer exists reads as
        absent, so a deleted chat can't strand a channel."""
        session_id = store.get_channel_session(channel_key)
        return session_id if session_id and store.session_exists(session_id) else None

    def get_or_create_channel_session(self, channel_key: str, title: str,
                                       model_endpoint_id: Optional[str] = None) -> str:
        """Stable session-per-channel mapping (e.g. "discord:<bot_id>" -> one
        shared session), so a comms adapter's conversation persists across
        restarts and shows up in the normal Chats sidebar like any other
        session — channels are just another way to reach the same one
        JARVIS, not a separate conversation store. Generic across channels
        on purpose (Phase 4's Discord adapter is the first caller;
        Telegram/others later reuse this unchanged per the channel-agnostic
        core).

        model_endpoint_id (David's ask 2026-09-01): a channel session has no
        model chosen by default like any other session — without this, every
        message through a channel hit chat_service's "no model added"
        message, since there was never a UI to pick one for a channel the
        way the chat model picker does for a normal session. Only applied
        when the session is first created; changing a channel's configured
        default model later doesn't retroactively move an existing pinned
        session (matches the chat model picker's own "explicit pick,
        doesn't silently change" behavior)."""
        existing = self.get_channel_session_id(channel_key)
        if existing:
            return existing

        session = self.create_session(title)
        if model_endpoint_id:
            self.set_model_endpoint(session["id"], model_endpoint_id)
        store.set_channel_session(channel_key, session["id"])
        return session["id"]


session_manager = SessionManager()
