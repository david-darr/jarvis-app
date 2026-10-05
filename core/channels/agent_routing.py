"""Reaching agents from a comms channel (2026-10-05; spec: the vault note
"Agents - Phase 1b Discord (Build Spec)").

Channel-agnostic on purpose: Discord is the first caller
(core/channels/discord_channel.py), and any later connector reuses these
three things unchanged:

- match_agent(): does a message start with an agent's name?
- direct(): talk to that agent (its own chat, in Auto) or give it a job.
- answer_reply(): a reply to one of the agent's notifications answers the
  question, report or result that notification carried.

Who may call these is the caller's decision, and it must be narrow: agents
run in Auto, so directing one approves what it then does. Discord allows
only a bot's own allowed user, never an open channel.
"""
import re
from typing import Optional

from core.session_manager import session_manager
from services import chat_service
from services.agent_service import CODE_RE, agent_service
from services.task_service import task_service

_SEPARATORS = ",: \t\r\n"
_JOB = re.compile(r"job\s*:\s*(.+)", re.IGNORECASE | re.DOTALL)
CARD_NAME_LIMIT = 60


def match_agent(text: str) -> Optional[tuple[dict, str]]:
    """(agent, the rest of the message) when the message starts with an
    agent's whole name ("Scout, ...", "@Scout ...", "scout: ..."), else
    None. Longest names first, so "Scout Two" is not taken as "Scout"."""
    stripped = (text or "").lstrip()
    if stripped.startswith("@"):
        stripped = stripped[1:]
    for agent in sorted(agent_service.list_agents(), key=lambda a: len(a["name"]), reverse=True):
        name = agent["name"]
        if stripped[:len(name)].lower() != name.lower():
            continue
        rest = stripped[len(name):]
        if rest == "" or rest[0] in _SEPARATORS:
            return agent, rest.lstrip(_SEPARATORS).strip()
    return None


async def direct(agent: dict, rest: str, channel_key: str, channel_label: str,
                 attachment_ids: Optional[list[str]] = None) -> str:
    """What the agent says back. "job: ..." becomes a Ready card for it;
    anything else is a turn in the agent's own chat for this channel."""
    job = _JOB.match(rest)
    if job:
        return _queue_job(agent, job.group(1).strip())
    if not rest and not attachment_ids:
        return f"{agent['name']} here. What do you need?"
    endpoint_id = agent_service.chat_endpoint(agent)
    if endpoint_id is None:
        return f"{agent['name']} has no model to answer with. Pick one on its page in JARVIS."
    session_id = session_manager.get_or_create_channel_session(
        f"{channel_key}:agent:{agent['id']}", f"{agent['name']} on {channel_label}", model_endpoint_id=endpoint_id)
    agent_service.make_agent_chat(session_id, agent)
    # Admin: only the channel's trusted owner gets here, and agent chats run
    # in Auto, which applies to admin chats (core/permissions.py).
    return await chat_service.send_message(session_id, rest, attachment_ids or [], is_admin=True)


def _queue_job(agent: dict, text: str) -> str:
    if not text:
        return "Say what the job is after 'job:'."
    may_run, why_not = agent_service.can_run(agent["id"])
    first_line = text.splitlines()[0].strip()
    name = first_line if len(first_line) <= CARD_NAME_LIMIT else first_line[:CARD_NAME_LIMIT - 1].rstrip() + "…"
    task_service.create_task(name, text, "card", status="ready", agent_id=agent["id"])
    if not may_run:
        return f"Queued for {agent['name']}: \"{name}\". It will start when it can ({why_not})."
    return f"Queued for {agent['name']}: \"{name}\". I'll tell you when the result is ready."


def answer_reply(notification_text: str, reply: str) -> Optional[str]:
    """When a message replies to an agent notification, answer what it
    carried and say what happens next. None when the replied-to message
    carries no code (so the caller handles the message as usual)."""
    found = agent_service.resolve_code(notification_text)
    if found is None:
        return None if not CODE_RE.search(notification_text or "") else \
            "That has already been answered, or is no longer waiting."
    kind, target = found
    agent = agent_service.get(target["agent_id"]) or {"name": "The agent"}
    try:
        if kind == "review":
            return f"{agent['name']}: {agent_service.review(target['id'], reply)}."
        return f"{agent['name']}: {agent_service.answer_item(target['id'], 'reply', reply)}."
    except (KeyError, ValueError) as e:
        return f"Couldn't do that: {e}."
