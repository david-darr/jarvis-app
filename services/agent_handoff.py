"""A card handed to an agent, with its progress and replies in the source chat."""
import uuid

from core import session_manager_store as store
from core.session_manager import session_manager
from core.untrusted import wrap_untrusted
from services.agent_service import agent_service
from services.task_service import task_service


def recent_context(session_id: str) -> list[dict]:
    session = session_manager.get_session(session_id)
    if session is None:
        raise ValueError("The source chat no longer exists")
    return [m for m in session.get("messages", []) if m.get("role") in ("user", "assistant")
            and m.get("type") != "handoff"][-10:]


def hand_off(agent_id: str, work: str, source_session_id: str, context: list[dict]) -> dict:
    agent = agent_service.get(agent_id)
    if agent is None:
        raise ValueError("The agent no longer exists")
    if session_manager.get_session(source_session_id) is None:
        raise ValueError("The source chat no longer exists")
    if not isinstance(work, str) or not work.strip():
        raise ValueError("Say what work to hand over")
    lines = [f"{m.get('agent_name') or m.get('role')}: {str(m.get('content') or '')[:1200]}"
             for m in context[-10:] if m.get("role") in ("user", "assistant") and m.get("type") != "handoff"]
    framed = wrap_untrusted("recent context from another conversation", "\n\n".join(lines)[:12000])
    card = task_service.create_task(work.strip().splitlines()[0][:60], work.strip(), "card",
                                    status="ready", agent_id=agent_id, reply_to=source_session_id, context=framed)
    event = {"type": "handoff", "id": uuid.uuid4().hex[:12], "agent_id": agent_id,
             "agent_name": agent["name"], "agent_color": agent.get("color"), "card_id": card["id"],
             "handoff_status": "queued", "note": queue_note(agent_id)}
    session_manager.append_message(source_session_id, "assistant", f"Handed to {agent['name']}", extra=event)
    return event


def queue_note(agent_id: str) -> str:
    may_run, reason = agent_service.can_run(agent_id)
    agent = agent_service.get(agent_id)
    if not may_run and agent and agent.get("enabled") and "today" in reason:
        return f"{agent['name']} is at today's limit"
    return reason if not may_run else ""


def _update(session_id: str, predicate, fields: dict) -> None:
    session = session_manager.get_session(session_id)
    for index, message in enumerate((session or {}).get("messages", [])):
        if predicate(message):
            store.update_message_fields(session_id, index, fields)


def sync_card(card: dict) -> None:
    session_id = card.get("reply_to")
    if not session_id:
        return
    status = {"ready": "queued", "backlog": "queued", "running": "working", "review": "done",
              "done": "done", "blocked": "failed"}[card["status"]]
    if status != "failed" and agent_service.open_items_for_card(card["id"]):
        status = "needs_you"
    _update(session_id, lambda m: m.get("type") == "handoff" and m.get("card_id") == card["id"],
            {"handoff_status": status, "note": queue_note(card["agent_id"]) if status == "queued" else ""})


def _post(card: dict, text: str, key: str, **extra) -> None:
    session_id = card.get("reply_to")
    session = session_manager.get_session(session_id) if session_id else None
    if session is None or any(m.get("handoff_message_id") == key for m in session.get("messages", [])):
        return
    agent = agent_service.get(card["agent_id"]) or {"name": "Agent"}
    session_manager.append_message(session_id, "assistant", text, extra={
        "agent_id": card["agent_id"], "agent_name": agent["name"], "agent_color": agent.get("color"),
        "card_id": card["id"], "handoff_message_id": key, **extra})
    session_manager.set_untrusted_context(session_id, "agent reply from another conversation")


def finished(card: dict, output: str) -> None:
    if card.get("reply_to"):
        _post(card, output, f"handoff:{card['id']}:{card.get('run_id') or card.get('attempts')}")
        sync_card(card)


def mirror_question(item: dict) -> None:
    card = task_service.get_task(item.get("card_id")) if item.get("card_id") else None
    if card and card.get("reply_to") and item["kind"] == "question":
        _post(card, item["title"] + ("\n\n" + item["body"] if item.get("body") else ""),
              f"handoff:question:{item['id']}", inbox_item_id=item["id"], question_status=item["status"])
        sync_card(card)


def question_resolved(item: dict) -> None:
    card = task_service.get_task(item.get("card_id")) if item.get("card_id") else None
    if card and card.get("reply_to"):
        _update(card["reply_to"], lambda m: m.get("inbox_item_id") == item["id"],
                {"question_status": item["status"]})
        sync_card(card)
