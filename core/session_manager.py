"""Chat session persistence — multiple named sessions, each with its own
message history, matching the "multiple open chats you can enter into or
search for/create" requirement from JARVIS Plan's tab-content scoping.

JSON-file-backed for this phase (one file per session under data/sessions/,
plus a small index file for listing without loading every session body) —
consistent with the atomic-write pattern already used for auth. A real
database is a later-phase concern if/when session volume or search needs
outgrow this; not worth the added complexity yet.
"""
import os
import time
import uuid
from typing import Optional

from core.atomic_io import read_json, write_json_atomic
from core.constants import DATA_DIR

SESSIONS_DIR = os.path.join(DATA_DIR, "sessions")
SESSIONS_INDEX_FILE = os.path.join(DATA_DIR, "sessions_index.json")
CHANNEL_SESSIONS_FILE = os.path.join(DATA_DIR, "channel_sessions.json")


def _session_path(session_id: str) -> str:
    return os.path.join(SESSIONS_DIR, f"{session_id}.json")


class SessionManager:
    def __init__(self) -> None:
        os.makedirs(SESSIONS_DIR, exist_ok=True)
        self._index: dict = read_json(SESSIONS_INDEX_FILE, {})

    def _save_index(self) -> None:
        write_json_atomic(SESSIONS_INDEX_FILE, self._index)

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
            # Which Settings > Integrations MCP Tool Servers this chat can
            # reference (David's ask 2026-08-31, matching Claude's per-
            # conversation connector toggle) — None = all registered ones
            # (matches pre-existing global behavior); a list restricts to
            # just those ids.
            "enabled_integration_ids": None,
        }
        write_json_atomic(_session_path(session_id), session)
        self._index[session_id] = {
            "title": title,
            "starred": False,
            "created_at": now,
            "updated_at": now,
            "message_count": 0,
            "model_endpoint_id": None,
        }
        self._save_index()
        return session

    def list_sessions(self) -> list[dict]:
        """Starred first, then newest-first within each group — metadata only
        (no message bodies). Right-click star/delete is David's ask, 2026-08-31."""
        items = [{"id": sid, **meta} for sid, meta in self._index.items()]
        return sorted(items, key=lambda s: (not s.get("starred", False), -s["updated_at"]))

    def set_starred(self, session_id: str, starred: bool) -> dict:
        if session_id not in self._index:
            raise KeyError(f"no such session: {session_id}")
        session = self.get_session(session_id)
        session["starred"] = starred
        write_json_atomic(_session_path(session_id), session)
        self._index[session_id]["starred"] = starred
        self._save_index()
        return session

    def get_session(self, session_id: str) -> Optional[dict]:
        if session_id not in self._index:
            return None
        return read_json(_session_path(session_id), None)

    def append_message(self, session_id: str, role: str, content: str) -> None:
        session = self.get_session(session_id)
        if session is None:
            raise KeyError(f"no such session: {session_id}")
        session["messages"].append({"role": role, "content": content, "ts": time.time()})
        session["updated_at"] = time.time()

        # Auto-title from the first user message, same idea as most chat UIs
        # (Odysseus included) — a session named "New Chat" forever isn't
        # findable in a sidebar list. Set on the session dict itself before
        # writing it, not just the index — the two must never disagree.
        if session["title"] == "New Chat" and role == "user":
            session["title"] = content[:60]

        write_json_atomic(_session_path(session_id), session)

        self._index[session_id]["title"] = session["title"]
        self._index[session_id]["updated_at"] = session["updated_at"]
        self._index[session_id]["message_count"] = len(session["messages"])
        self._save_index()

    def set_model_endpoint(self, session_id: str, model_endpoint_id: Optional[str]) -> dict:
        """Pin a session to a specific "bring your own model" endpoint (see
        core/model_endpoints.py), or back to None for the default Claude Agent
        SDK brain. chat_service picks the right Brain implementation off this
        field per session — never mid-session-swap the live connection."""
        if session_id not in self._index:
            raise KeyError(f"no such session: {session_id}")
        session = self.get_session(session_id)
        session["model_endpoint_id"] = model_endpoint_id
        write_json_atomic(_session_path(session_id), session)
        self._index[session_id]["model_endpoint_id"] = model_endpoint_id
        self._save_index()
        return session

    def set_workspace(self, session_id: str, workspace_dir: Optional[str]) -> dict:
        """Pin a session's agent tools to a specific folder (see
        core/workspace.py's vet_workspace — the caller must vet before
        calling this), or clear back to None for the default vault scope."""
        if session_id not in self._index:
            raise KeyError(f"no such session: {session_id}")
        session = self.get_session(session_id)
        session["workspace_dir"] = workspace_dir
        write_json_atomic(_session_path(session_id), session)
        return session

    def set_integrations(self, session_id: str, enabled_integration_ids: Optional[list[str]]) -> dict:
        """Restrict which MCP Tool Server integrations this chat can
        reference (David's ask 2026-08-31), or clear back to None for "all
        registered ones" — same distinction Claude's own per-conversation
        connector toggle makes."""
        if session_id not in self._index:
            raise KeyError(f"no such session: {session_id}")
        session = self.get_session(session_id)
        session["enabled_integration_ids"] = enabled_integration_ids
        write_json_atomic(_session_path(session_id), session)
        return session

    def rename_session(self, session_id: str, title: str) -> None:
        if session_id not in self._index:
            raise KeyError(f"no such session: {session_id}")
        session = self.get_session(session_id)
        session["title"] = title
        write_json_atomic(_session_path(session_id), session)
        self._index[session_id]["title"] = title
        self._save_index()

    def delete_session(self, session_id: str) -> None:
        if session_id in self._index:
            del self._index[session_id]
            self._save_index()
        path = _session_path(session_id)
        if os.path.exists(path):
            os.remove(path)

    def get_channel_session_id(self, channel_key: str) -> Optional[str]:
        """Look up a channel's already-pinned session without creating one
        (David's ask 2026-09-01, real gap found live: changing a Discord
        bot's default model in Settings only ever affected a session
        created *after* that point — an already-existing channel
        conversation, exactly what David actually hit, kept whatever model
        it started with, silently ignoring the new Settings value). Used by
        routes/settings_routes.py to also push a model change onto an
        existing session, not just future ones."""
        mapping: dict = read_json(CHANNEL_SESSIONS_FILE, {})
        session_id = mapping.get(channel_key)
        return session_id if session_id and session_id in self._index else None

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
        mapping: dict = read_json(CHANNEL_SESSIONS_FILE, {})
        session_id = mapping.get(channel_key)
        if session_id and session_id in self._index:
            return session_id

        session = self.create_session(title)
        if model_endpoint_id:
            self.set_model_endpoint(session["id"], model_endpoint_id)
        mapping[channel_key] = session["id"]
        write_json_atomic(CHANNEL_SESSIONS_FILE, mapping)
        return session["id"]


session_manager = SessionManager()
