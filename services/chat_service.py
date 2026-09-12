"""Session-aware chat: one live Brain (Claude Agent SDK connection, or an
ExternalBrain for a session pinned to a "bring your own model" endpoint) per
open session, connected lazily on first message and kept alive for reuse so
conversation context carries across turns within that session. Replaces the
Phase 0 single-shared-Brain placeholder entirely, per the plan for that module.

David's ask 2026-08-31 (follow-up): JARVIS ships with NO default model —
Claude used to be the automatic fallback when a session had no
model_endpoint_id; now that's just "nothing chosen yet," and a message sent
before the user has added any model in Settings gets a canned reply telling
them to go add one, instead of silently spending a real Claude turn.
"""
from typing import AsyncIterator, Optional, Union

from claude_agent_sdk import CLIJSONDecodeError

from core import attachments, model_endpoints, token_usage
from core.brain import Brain
from core.codex_brain import CodexBrain
from core.external_brain import ExternalBrain
from core.session_manager import session_manager
from core.vault import resolve_vault_dir

AnyBrain = Union[Brain, ExternalBrain, CodexBrain]

_brains: dict[str, AnyBrain] = {}

NO_MODEL_MESSAGE = (
    "You haven't added a model yet. Go to Settings → Add Models to connect "
    "Claude Code CLI, Codex CLI, a local model (Ollama, llama.cpp, vLLM), or "
    "an API provider — then pick it from the model menu above the chat box."
)

# Found live 2026-09-08: a big-enough attachment (6 images in one Discord
# message) produces a single reply message from the Claude Code CLI larger
# than the SDK transport's per-JSON-line buffer cap (core/brain.py sets this
# to 10MB — see its own comment for why; the SDK's own default is 1MB),
# which raises CLIJSONDecodeError from deep inside the SDK's background
# reader — a fatal, connection-level failure, not a per-turn one. The brain
# stayed cached in _brains with its reader already dead, so every message
# afterward hit the same broken connection, got an immediate empty reply
# (StopAsyncIteration on the first read), and both send_message/
# stream_message's callers ended up trying to send that empty string —
# Discord rejects it outright, and it looked like the bot had gone silent.
# Any turn-level exception now evicts the session's brain so the next
# message reconnects instead of reusing a dead client, and this specific
# one gets an actionable reply pointing at the actual cause instead of the
# generic fallback.
ATTACHMENT_TOO_LARGE_MESSAGE = (
    "One of the attachments in that message produced a response too large for "
    "Claude Code to process in one go — enough images or large files in a "
    "single message can still blow past the buffer, even with real headroom. "
    "Try again with fewer or smaller files."
)


def _resolve_endpoint(session_id: str) -> Optional[dict]:
    """None means "nothing chosen" — covers both a session with no
    model_endpoint_id set at all, and one pointing at an endpoint the user
    has since deleted in Settings. Either way, the honest answer is "no
    model configured," not a silent fallback to anything."""
    session = session_manager.get_session(session_id)
    endpoint_id = (session or {}).get("model_endpoint_id")
    if not endpoint_id:
        return None
    return model_endpoints.get_endpoint(endpoint_id)


async def _get_brain(session_id: str, endpoint: dict, is_admin: bool = False) -> tuple[AnyBrain, bool]:
    """Returns (brain, just_created) — just_created tells the caller this is a
    fresh connection with no live conversation state yet, so it's the one
    moment a Claude-CLI Brain needs its prior transcript primed back in (see
    _prime_with_history)."""
    brain = _brains.get(session_id)
    if brain is not None:
        return brain, False

    session = session_manager.get_session(session_id)
    workspace_dir = (session or {}).get("workspace_dir")
    integration_ids = (session or {}).get("enabled_integration_ids")
    # Projects (David's ask 2026-09-12) — read once here rather than in each
    # Brain subclass, so every model kind picks it up the same way. See
    # core/projects.py's project_addendum() for what actually gets injected.
    project_id = (session or {}).get("project_id")
    if endpoint["kind"] == "claude_cli":
        brain = Brain(cwd_override=workspace_dir, integration_ids=integration_ids,
                       session_id=session_id, model=endpoint.get("model") or None, is_admin=is_admin,
                       project_id=project_id)
    elif endpoint["kind"] == "codex_cli":
        brain = CodexBrain(cwd_override=workspace_dir, session_id=session_id,
                            model=endpoint.get("model") or None, is_admin=is_admin, project_id=project_id)
    else:
        base_url, model, api_key, num_ctx = model_endpoints.resolve_runtime(endpoint["id"])
        brain = ExternalBrain(base_url, model, api_key, history=(session or {}).get("messages", []),
                               session_id=session_id, num_ctx=num_ctx, is_admin=is_admin, project_id=project_id)

    await brain.connect()
    _brains[session_id] = brain
    return brain, True


def _prime_with_history(session_id: str, just_created: bool, endpoint: dict, full_text: str) -> str:
    """A freshly (re)connected Claude-CLI Brain starts with zero memory of
    this session's prior turns — the Claude Agent SDK only keeps conversation
    state in-process, so a server restart (or any brain eviction) silently
    drops everything already said in this chat. That includes course-material
    notes school_service.sync_course_memory() seeds via append_message: those
    are written straight to the session's saved transcript WITHOUT ever going
    through a real brain turn, on the assumption (see that module's docstring)
    that "the note only becomes something the model actually reads once the
    user sends a real message" — true only if something actually replays the
    transcript, which nothing did. ExternalBrain already seeds its history on
    connect (its `history=` param); this is the same idea for the Claude-CLI
    path, applied once per (re)connect rather than every turn so a
    long-running session pays this cost only after it's actually needed."""
    if not just_created or endpoint["kind"] != "claude_cli":
        return full_text
    session = session_manager.get_session(session_id) or {}
    prior = session.get("messages", [])[:-1]  # exclude the message just appended for this turn
    if not prior:
        return full_text
    transcript = "\n\n".join(f'{m["role"]}: {m["content"]}' for m in prior)
    return (
        "[This chat has history from before this connection — context from "
        f"earlier in this same conversation, for your reference:]\n\n{transcript}"
        f"\n\n[End of prior context. Current message:]\n{full_text}"
    )


def _apply_attachments(session_id: str, text: str, attachment_ids: list[str] | None) -> str:
    """Copies staged attachments into the session's active cwd (workspace if
    set, else the vault) and appends a note listing their paths so the
    agent's own cwd-scoped file tools can read them — see core/attachments.py."""
    if not attachment_ids:
        return text
    session = session_manager.get_session(session_id) or {}
    cwd = session.get("workspace_dir") or resolve_vault_dir()
    names = attachments.resolve_for_turn(attachment_ids, session_id, cwd)
    if not names:
        return text
    note = "\n\n[Attached file(s), read with your file tools relative to your working directory: " + ", ".join(names) + "]"
    return text + note


async def send_message(session_id: str, text: str, attachment_ids: list[str] | None = None, is_admin: bool = False) -> str:
    session_manager.append_message(session_id, "user", text)
    endpoint = _resolve_endpoint(session_id)
    if endpoint is None:
        session_manager.append_message(session_id, "assistant", NO_MODEL_MESSAGE)
        return NO_MODEL_MESSAGE

    full_text = _apply_attachments(session_id, text, attachment_ids)
    brain, just_created = await _get_brain(session_id, endpoint, is_admin)
    full_text = _prime_with_history(session_id, just_created, endpoint, full_text)
    try:
        reply = await brain.run_turn(full_text)
    except CLIJSONDecodeError:
        await close_session_brain(session_id)
        reply = ATTACHMENT_TOO_LARGE_MESSAGE
    except Exception:
        await close_session_brain(session_id)
        raise
    token_usage.record_usage(endpoint["id"], getattr(brain, "last_usage", None))
    session_manager.append_message(session_id, "assistant", reply)
    return reply


async def stream_message(session_id: str, text: str, attachment_ids: list[str] | None = None, is_admin: bool = False) -> AsyncIterator[str]:
    session_manager.append_message(session_id, "user", text)
    endpoint = _resolve_endpoint(session_id)
    if endpoint is None:
        session_manager.append_message(session_id, "assistant", NO_MODEL_MESSAGE)
        yield NO_MODEL_MESSAGE
        return

    full_text = _apply_attachments(session_id, text, attachment_ids)
    brain, just_created = await _get_brain(session_id, endpoint, is_admin)
    full_text = _prime_with_history(session_id, just_created, endpoint, full_text)

    reply_parts: list[str] = []
    try:
        async for chunk in brain.run_turn_stream(full_text):
            reply_parts.append(chunk)
            yield chunk
    except CLIJSONDecodeError:
        await close_session_brain(session_id)
        reply_parts.append(ATTACHMENT_TOO_LARGE_MESSAGE)
        yield ATTACHMENT_TOO_LARGE_MESSAGE
    except Exception:
        await close_session_brain(session_id)
        raise

    token_usage.record_usage(endpoint["id"], getattr(brain, "last_usage", None))
    session_manager.append_message(session_id, "assistant", "".join(reply_parts))


async def close_session_brain(session_id: str) -> None:
    brain = _brains.pop(session_id, None)
    if brain is not None:
        await brain.disconnect()


async def shutdown() -> None:
    for session_id in list(_brains.keys()):
        await close_session_brain(session_id)
