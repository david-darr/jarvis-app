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
import asyncio
import logging
import time
from typing import AsyncIterator, Optional, Union
from contextlib import asynccontextmanager
from fastapi import HTTPException

from claude_agent_sdk import CLIJSONDecodeError

from core import attachments, model_catalog, model_endpoints, permissions, token_usage
from core.brain import Brain
from core.codex_brain import CodexBrain
from core.external_brain import ExternalBrain
from core.session_manager import session_manager
from core.vault import resolve_vault_dir

logger = logging.getLogger(__name__)

AnyBrain = Union[Brain, ExternalBrain, CodexBrain]

_brains: dict[str, AnyBrain] = {}
_busy: set[str] = set()


def is_busy(session_id: str) -> bool:
    return session_id in _busy


@asynccontextmanager
async def session_operation(session_id: str):
    """Claim before the first await; never replace a running session brain."""
    if session_manager.get_session(session_id) is None:
        raise HTTPException(404, "session not found")
    if session_id in _busy:
        raise HTTPException(409, "This chat is busy. Wait for the current response to finish.")
    _busy.add(session_id)
    try:
        yield
    finally:
        _busy.discard(session_id)

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

    brain = _build_brain(endpoint, session_id=session_id, is_admin=is_admin)
    try:
        await brain.connect()
    except Exception:
        if not (isinstance(brain, Brain) and brain.resume_session_id):
            raise
        # The CLI refused the resume (session pruned, or started in another
        # working folder). Forget it and start fresh: _prime_with_history
        # then replays the saved transcript, exactly as before resume
        # existed, so the chat never gets stuck on a dead session id.
        logger.warning("claude resume of %s failed for chat %s; starting a fresh CLI session",
                       brain.resume_session_id, session_id)
        session_manager.set_claude_session(session_id, None)
        brain = _build_brain(endpoint, session_id=session_id, is_admin=is_admin)
        await brain.connect()
    _brains[session_id] = brain
    return brain, True


def _build_brain(endpoint: dict, session_id: Optional[str], is_admin: bool = False) -> AnyBrain:
    """Construct (but do not connect) a brain for an endpoint.

    session_id=None builds one detached from any conversation: no cross-session
    search context, no replayed history. That is what the Open Mic summariser
    wants, since it must read the saved transcript rather than lean on what a
    live brain happens to remember.
    """
    session = session_manager.get_session(session_id) if session_id else None
    workspace_dir = (session or {}).get("workspace_dir")
    integration_ids = (session or {}).get("enabled_integration_ids")
    # Projects (David's ask 2026-09-12) — read once here rather than in each
    # Brain subclass, so every model kind picks it up the same way. See
    # core/projects.py's project_addendum() for what actually gets injected.
    project_id = (session or {}).get("project_id")
    override = (session or {}).get("model_override")
    cli_model = endpoint.get("model") if override is None else override
    # Reasoning effort (David's ask 2026-09-15) — validated at the route
    # boundary against core/model_catalog.py, so by here it's either None
    # (send nothing, the pre-existing behaviour) or a value the provider
    # advertises for this model.
    effort = (session or {}).get("model_effort")
    if endpoint["kind"] == "claude_cli":
        return Brain(cwd_override=workspace_dir, integration_ids=integration_ids,
                     session_id=session_id, model=cli_model or None, is_admin=is_admin,
                     project_id=project_id, effort=effort,
                     resume_session_id=(session or {}).get("claude_session_id"))
    if endpoint["kind"] == "codex_cli":
        return CodexBrain(cwd_override=workspace_dir, session_id=session_id,
                          model=cli_model or None, is_admin=is_admin, project_id=project_id,
                          effort=effort)
    base_url, model, api_key, num_ctx = model_endpoints.resolve_runtime(endpoint["id"])
    # exclude_last: the current message is already saved by the time a brain
    # is built, and run_turn() adds it itself. Loading it here too sent it to
    # the model twice on every fresh connection (found 2026-09-22), and the
    # doubled request could never match the one rebuilt after a reconnect.
    return ExternalBrain(base_url, model, api_key, history=session_manager.effective_messages(session_id, exclude_last=True),
                         session_id=session_id, num_ctx=num_ctx, is_admin=is_admin, project_id=project_id)


def _prime_with_history(session_id: str, just_created: bool, endpoint: dict, full_text: str,
                        brain: Optional[AnyBrain] = None) -> str:
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
    if isinstance(brain, Brain) and brain.resume_session_id:
        # Resumed: the CLI already holds the conversation up to the point it
        # last saw. Hand over only what was saved after that without going
        # through it - a seeded course note, a stopped reply - normally
        # nothing. The current message (saved last) is excluded.
        session = session_manager.get_session(session_id) or {}
        unseen = session.get("messages", [])[session.get("claude_synced_through", 0):-1]
        unseen = [m for m in unseen if m.get("content")]
        if not unseen:
            return full_text
        transcript = "\n\n".join(f'{m["role"]}: {m["content"]}' for m in unseen)
        return (
            "[Messages added to this chat since your last turn, for context:]\n\n"
            f"{transcript}\n\n[End of added messages. Current message:]\n{full_text}"
        )
    prior = session_manager.effective_messages(session_id, exclude_last=True)
    if not prior:
        return full_text
    transcript = "\n\n".join(f'{m["role"]}: {m["content"]}' for m in prior)
    return (
        "[This chat has history from before this connection — context from "
        f"earlier in this same conversation, for your reference:]\n\n{transcript}"
        f"\n\n[End of prior context. Current message:]\n{full_text}"
    )


def _remember_claude_session(session_id: str, brain: Optional[AnyBrain], cancelled: bool = False,
                             succeeded: bool = True) -> None:
    """Record which CLI session a Claude chat's turns run in, and that it has
    now seen every saved message, so the next connect resumes instead of
    replaying. Called after a turn is saved.

    A stopped turn still counts as seen: the CLI received the message. A
    turn that failed on a resumed session that never completed a turn is
    the one case that forgets the session, so a broken resume cannot keep
    failing every message after it."""
    if not isinstance(brain, Brain):
        return
    if not succeeded and not cancelled:
        if brain.cli_session_id is None and brain.resume_session_id:
            session_manager.set_claude_session(session_id, None)
        return
    cli_id = brain.cli_session_id or (brain.resume_session_id if cancelled else None)
    if cli_id:
        session = session_manager.get_session(session_id) or {}
        session_manager.set_claude_session(session_id, cli_id, len(session.get("messages", [])))


def _apply_attachments(session_id: str, text: str, attachment_ids: list[str] | None) -> str:
    """Copies staged attachments into the session's active cwd (workspace if
    set, else the vault) and appends a note listing their paths so the
    agent's own cwd-scoped file tools can read them — see core/attachments.py."""
    if not attachment_ids:
        return text
    session = session_manager.get_session(session_id) or {}
    cwd = session.get("workspace_dir") or resolve_vault_dir()
    names, warnings = attachments.resolve_for_turn(attachment_ids, session_id, cwd)
    if not names:
        return text
    note = "\n\n[Attached file(s), read with your file tools relative to your working directory: " + ", ".join(names) + "]"
    if warnings:
        note += " (" + "; ".join(warnings) + ")"
    return text + note


# Spoken replies are heard, not read, and David's note after the first real
# Open Mic session was that the answers were far too long for a back-and-forth
# (2026-09-15). This is the same instinct as voice-line's SPOKEN_DISCIPLINE,
# cut down hard: voice-line is a standalone assistant that has to fill dead air
# during tool calls, whereas an Open Mic chat is a conversation where the other
# person is waiting to speak.
#
# Deliberately brief. A long style preamble on every turn competes with the
# actual message for attention and eats the context window a spoken session
# fills quickly anyway.
OPEN_MIC_DISCIPLINE = (
    "\n\n[This is a live spoken conversation. Your reply is read aloud and the "
    "other person is waiting to answer. Keep it to a couple of sentences - say "
    "the answer, not the reasoning, and stop. No markdown, no lists, no code "
    "read aloud: describe it instead. Ask if they want the detail rather than "
    "giving it unprompted. If a real answer genuinely needs length, say the "
    "short version aloud and offer the rest.]"
)


def _apply_open_mic_discipline(session_id: str, full_text: str) -> str:
    """Appends the spoken-reply instruction when this session is in Open Mic.

    Applied to what is SENT, never to what is stored — same split as
    _apply_attachments above. The transcript keeps the user's actual words, so
    leaving Open Mic (and the summary that follows) sees a clean conversation
    rather than one with a style note bolted onto every line.

    Re-sent each turn rather than once at connection: the brain is long-lived,
    and a single instruction at the top of a spoken session is reliably
    forgotten by the tenth exchange, which is exactly when the replies growing
    long is most annoying.
    """
    session = session_manager.get_session(session_id) or {}
    if not session.get("open_mic"):
        return full_text
    return full_text + OPEN_MIC_DISCIPLINE


async def send_message(session_id: str, text: str, attachment_ids: list[str] | None = None, is_admin: bool = False) -> str:
    async with session_operation(session_id):
        return await _send_message(session_id, text, attachment_ids, is_admin)


async def _send_message(session_id: str, text: str, attachment_ids: list[str] | None = None, is_admin: bool = False) -> str:
    session_manager.append_message(session_id, "user", text)
    endpoint = _resolve_endpoint(session_id)
    if endpoint is None:
        session_manager.append_message(session_id, "assistant", NO_MODEL_MESSAGE)
        return NO_MODEL_MESSAGE

    full_text = _apply_attachments(session_id, text, attachment_ids)
    brain, just_created = await _get_brain(session_id, endpoint, is_admin)
    full_text = _prime_with_history(session_id, just_created, endpoint, full_text, brain)
    full_text = _apply_open_mic_discipline(session_id, full_text)
    try:
        reply = await brain.run_turn(full_text)
    except CLIJSONDecodeError:
        await close_session_brain(session_id)
        reply = ATTACHMENT_TOO_LARGE_MESSAGE
    except Exception:
        _remember_claude_session(session_id, brain, succeeded=False)
        await close_session_brain(session_id)
        raise
    _record_turn_telemetry(session_id, endpoint, brain)
    session_manager.append_message(session_id, "assistant", reply)
    _remember_claude_session(session_id, brain)
    return reply


async def stream_message(session_id: str, text: str, attachment_ids: list[str] | None = None, is_admin: bool = False) -> AsyncIterator[str]:
    async with session_operation(session_id):
        async for chunk in _stream_message(session_id, text, attachment_ids, is_admin):
            yield chunk


async def _stream_message(session_id: str, text: str, attachment_ids: list[str] | None = None, is_admin: bool = False) -> AsyncIterator[str]:
    session_manager.append_message(session_id, "user", text)
    endpoint = _resolve_endpoint(session_id)
    if endpoint is None:
        session_manager.append_message(session_id, "assistant", NO_MODEL_MESSAGE)
        yield NO_MODEL_MESSAGE
        return

    reply_parts: list[str] = []
    brain = None
    try:
        full_text = _apply_attachments(session_id, text, attachment_ids)
        brain, just_created = await _get_brain(session_id, endpoint, is_admin)
        full_text = _prime_with_history(session_id, just_created, endpoint, full_text, brain)
        full_text = _apply_open_mic_discipline(session_id, full_text)
        async for item in _stream_with_permission_prompts(session_id, brain, full_text):
            if isinstance(item, str):
                reply_parts.append(item)
            yield item
    except CLIJSONDecodeError:
        await close_session_brain(session_id)
        reply_parts.append(ATTACHMENT_TOO_LARGE_MESSAGE)
        yield ATTACHMENT_TOO_LARGE_MESSAGE
    except BaseException as exc:
        # Includes client cancellation: preserve the visible partial answer.
        session_manager.append_message(session_id, "assistant", "".join(reply_parts), status="interrupted")
        _remember_claude_session(session_id, brain, succeeded=False,
                                 cancelled=isinstance(exc, (asyncio.CancelledError, GeneratorExit)))
        await close_session_brain(session_id)
        raise

    _record_turn_telemetry(session_id, endpoint, brain)
    session_manager.append_message(session_id, "assistant", "".join(reply_parts))
    _remember_claude_session(session_id, brain)


async def _stream_with_permission_prompts(session_id: str, brain, full_text: str):
    """Reply text, plus any permission request raised while producing it.

    A model waiting on approval produces nothing, so simply iterating the
    reply would sit silent until the request timed out. Watching the brain and
    the request queue together is what lets the prompt reach the person during
    the turn it belongs to. Text is yielded as a string exactly as before; a
    request is yielded as a dict, and only routes/chat_routes.py consumes this.
    """
    queue = permissions.open_channel(f"chat:{session_id}")
    replies = brain.run_turn_stream(full_text).__aiter__()
    next_chunk = asyncio.ensure_future(anext(replies))
    next_ask = asyncio.ensure_future(queue.get())
    try:
        while True:
            done, _ = await asyncio.wait({next_chunk, next_ask}, return_when=asyncio.FIRST_COMPLETED)
            if next_ask in done:
                yield {"permission": next_ask.result()}
                next_ask = asyncio.ensure_future(queue.get())
            if next_chunk in done:
                try:
                    chunk = next_chunk.result()
                except StopAsyncIteration:
                    return
                yield chunk
                next_chunk = asyncio.ensure_future(anext(replies))
    finally:
        next_ask.cancel()
        if not next_chunk.done():
            next_chunk.cancel()
        # Closing denies anything still waiting: a window that went away
        # cannot approve, and silence must never mean yes.
        permissions.close_channel(f"chat:{session_id}")
        await asyncio.gather(next_ask, next_chunk, return_exceptions=True)


def _record_turn_telemetry(session_id: str, endpoint: dict, brain) -> None:
    """Both post-turn bookkeeping jobs in one place, called from the
    streaming and non-streaming paths alike so they can't drift apart.

    Two genuinely different measurements come off the same usage payload:
    record_usage() accumulates lifetime spend per endpoint (the Home tab's
    card), while the context state is a point-in-time occupancy that
    replaces its predecessor every turn (the chat header's meter). See
    core/token_usage.py's extract_context_tokens() for why one can't be
    derived from the other.

    Deliberately non-fatal: the turn has already succeeded and its reply is
    about to be saved, so a telemetry failure must never turn a good answer
    into an error the user sees.
    """
    usage = getattr(brain, "last_usage", None)
    try:
        token_usage.record_usage(endpoint["id"], usage)
    except Exception:
        logger.exception("record_usage failed for endpoint %s", endpoint.get("id"))
    try:
        model_id = getattr(brain, "model", None) or endpoint.get("model") or None
        capacity = model_catalog.context_capacity(endpoint.get("kind"), model_id)
        if capacity is None and endpoint.get("kind") == "local" and endpoint.get("num_ctx"):
            # A local endpoint's configured num_ctx IS its real context
            # ceiling (core/model_endpoints.py sets and enforces it), so
            # this is a measured capacity rather than a curated guess —
            # flagged accordingly.
            capacity = {"effective": int(endpoint["num_ctx"]), "estimated": False, "source": "endpoint_num_ctx"}
        session_manager.set_context_state(session_id, token_usage.build_context_state(usage, capacity, model_id))
    except Exception:
        logger.exception("context state update failed for session %s", session_id)


async def close_session_brain(session_id: str) -> None:
    brain = _brains.pop(session_id, None)
    if brain is not None:
        await brain.disconnect()


async def shutdown() -> None:
    for session_id in list(_brains.keys()):
        await close_session_brain(session_id)

# -- Open Mic (David's ask 2026-09-15) ---------------------------------------

SUMMARY_PROMPT = (
    "Below is a spoken conversation between a user and their assistant, transcribed from voice. "
    "Rewrite it as a compact summary that preserves everything a later reply would need: decisions made, "
    "facts established, questions still open, and anything the user asked to be remembered. "
    "Write it as notes, not dialogue. Do not add anything that was not said. "
    "If nothing of substance was discussed, say so in one line." + chr(10) * 2
)


async def summarise_open_mic(session_id: str) -> dict:
    """Fold a spoken stretch into a summary when Open Mic ends.

    The turns were ordinary messages while they happened, which is what makes
    leaving the mode leave a real conversation behind. But spoken exchanges run
    long and repetitive - "sorry, say that again", half sentences, filler - and
    carrying all of it forward would spend the context window on transcription
    artefacts. So the stretch is replaced by notes covering what was actually
    established.

    Deliberately summarised by a FRESH brain rather than the session's own: a
    live brain already holds this conversation, so asking it to summarise would
    both pollute its history with the request and bias it toward what it
    remembers rather than what the transcript says. The summary is derived from
    the saved messages only.

    Never raises. A failed summary leaves the raw turns in place, which is the
    safe direction - losing the conversation would be far worse than leaving it
    verbose.
    """
    session = session_manager.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    start = session.get("open_mic_started_at")
    messages = session.get("messages", [])
    if start is None or start >= len(messages):
        session_manager.set_open_mic(session_id, False)
        return {"summarised": False, "reason": "nothing was spoken"}

    spoken = messages[start:]
    # One exchange is not worth compressing, and a summary of it would be
    # longer than the thing itself.
    if len(spoken) < 4:
        session_manager.set_open_mic(session_id, False)
        return {"summarised": False, "reason": "too short to summarise"}

    endpoint = _resolve_endpoint(session_id)
    if endpoint is None:
        session_manager.set_open_mic(session_id, False)
        return {"summarised": False, "reason": "no model available"}

    transcript = (chr(10) * 2).join(f'{m["role"]}: {m["content"]}' for m in spoken if m.get("content"))
    brain = None
    try:
        brain = _build_brain(endpoint, session_id=None, is_admin=False)
        await brain.connect()
        summary = (await brain.run_turn(SUMMARY_PROMPT + transcript)).strip()
    except Exception:
        logger.exception("open mic: summary failed for %s", session_id)
        session_manager.set_open_mic(session_id, False)
        return {"summarised": False, "reason": "the summary could not be generated"}
    finally:
        if brain is not None:
            try:
                await brain.disconnect()
            except Exception:
                logger.exception("open mic: summary brain failed to disconnect")

    if not summary:
        session_manager.set_open_mic(session_id, False)
        return {"summarised": False, "reason": "the summary came back empty"}

    # Marked as what it is. A reader scrolling back should be able to tell
    # this is a condensed record rather than something either party said.
    session_manager.replace_messages(session_id, start, [{
        "role": "assistant",
        "content": "**Voice conversation summary**" + chr(10) * 2 + summary,
        "ts": time.time(),
        "status": "complete",
        "open_mic_summary": True,
    }])
    session_manager.set_open_mic(session_id, False)
    return {"summarised": True, "replaced": len(spoken)}

# -- Compact (David's ask 2026-09-21) ----------------------------------------
# A user-triggered "shrink this chat's context" action, distinct from Open
# Mic's summary above in the one way that matters: nothing is ever deleted or
# overwritten. session_manager.compact_session() only flags old messages
# archived and appends a checkpoint; effective_messages() is what actually
# changes what gets sent to the model on later turns. The full transcript —
# archived messages included — stays exactly where it was, readable and
# searchable, for the life of the chat.

# Kept live/uncompacted on every compaction so the most recent exchange stays
# exact rather than paraphrased, the same "protected tail" idea Hermes Agent's
# own ContextCompressor uses (see Harness Architecture Ideas' independent
# verification pass) — sized small since jarvis-app compaction is manual and
# infrequent, not a per-turn budget walk.
COMPACTION_TAIL_KEEP = 6

COMPACTION_PROMPT = (
    "Below is a conversation so far. Write a structured summary that preserves everything a later "
    "reply would need to continue this conversation seamlessly: decisions made, facts established, "
    "commitments made, questions still open, and any pending or unfinished work. Write it as reference "
    "notes, not dialogue. Do not add anything that was not actually said or done. Be thorough - "
    "nothing important should be lost, even if the summary runs long." + chr(10) * 2
)


async def compact_session(session_id: str) -> dict:
    async with session_operation(session_id):
        return await _compact_session(session_id)


async def _compact_session(session_id: str) -> dict:
    session = session_manager.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    endpoint = _resolve_endpoint(session_id)
    if endpoint is None:
        raise HTTPException(status_code=400, detail="add a model to this chat before compacting it")

    messages = session.get("messages", [])
    compactions = session.get("compactions") or []
    prior_through = compactions[-1]["through_index"] if compactions else 0
    through_index = max(prior_through, len(messages) - COMPACTION_TAIL_KEEP)
    if through_index <= prior_through:
        raise HTTPException(status_code=400, detail="not enough new conversation to compact yet")

    to_fold_in = messages[prior_through:through_index]
    transcript = (chr(10) * 2).join(f'{m["role"]}: {m["content"]}' for m in to_fold_in if m.get("content"))
    prompt = COMPACTION_PROMPT
    if compactions:
        prompt = (
            f"[Summary of everything before this point:]\n{compactions[-1]['summary']}"
            "\n\n[New conversation since then, to fold into that summary:]\n"
        ) + prompt
    prompt += transcript

    # Detached brain, same reasoning as summarise_open_mic above: the live
    # session brain already holds this conversation in its own state, so
    # asking it to summarize itself would pollute that state and bias the
    # summary toward what it remembers rather than what the transcript says.
    brain = _build_brain(endpoint, session_id=None, is_admin=False)
    try:
        await brain.connect()
        summary = (await brain.run_turn(prompt)).strip()
    finally:
        await brain.disconnect()
    if not summary:
        raise HTTPException(status_code=502, detail="the summary came back empty")

    session_manager.compact_session(session_id, through_index, summary)
    if endpoint["kind"] == "codex_cli":
        # Codex CLI owns its own server-side thread and cannot be seeded with
        # a summary mid-thread — the next turn starts a fresh one, primed
        # with the new compaction summary through the existing fresh-thread
        # path (core/codex_brain.py's is_fresh_thread branch, which already
        # calls effective_messages() the same as the other two brain kinds).
        session_manager.set_codex_thread_id(session_id, None)
    # Evicts any live brain holding the old, uncompacted history in memory
    # (ExternalBrain's message list; a connected Claude-CLI Brain) so the
    # next turn rebuilds from effective_messages() instead of growing what
    # it already had cached.
    await close_session_brain(session_id)

    session_manager.append_message(
        session_id, "assistant",
        "**Conversation compacted**" + chr(10) * 2 + summary,
    )
    return {"compacted_through": through_index, "archived": through_index - prior_through, "summary": summary}

