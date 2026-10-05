"""Agents: identities, memory, goals, work and the inbox (services/agent_service.py).

Admin only: agents always run in Auto (David, 2026-10-05), so whoever directs
them is approving everything they do, the same rule as a chat's Auto mode."""
import asyncio
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core import model_endpoints
from core.middleware import require_admin
from services.agent_service import agent_service
from services.task_service import task_service

router = APIRouter(prefix="/api/agents", tags=["agents"])


class AgentBody(BaseModel):
    name: Optional[str] = None
    role: Optional[str] = None
    instructions: Optional[str] = None
    endpoint_id: Optional[str] = None
    color: Optional[str] = None
    enabled: Optional[bool] = None
    daily_run_cap: Optional[int] = None
    deliver_to_channel: Optional[str] = None
    integration_ids: Optional[list[str]] = None


class MemoryBody(BaseModel):
    text: str


class GoalBody(BaseModel):
    name: str
    prompt: str
    schedule_kind: str  # "daily" or "interval"
    run_time: Optional[str] = None
    interval_seconds: Optional[int] = None
    report_when: Optional[str] = None


class CardBody(BaseModel):
    name: str
    prompt: str
    status: Optional[str] = "ready"


class AnswerBody(BaseModel):
    choice: str  # reply | dismiss
    text: Optional[str] = None


def _check_endpoint(endpoint_id: Optional[str]) -> None:
    if endpoint_id and model_endpoints.get_endpoint(endpoint_id) is None:
        raise HTTPException(status_code=400, detail="endpoint_id must name a model added in Settings")


def _owned(agent_id: str) -> list[dict]:
    return [t for t in task_service.list_tasks() if t.get("agent_id") == agent_id]


def _summary(agent: dict) -> dict:
    """The agent plus what its card on the Agents list shows."""
    owned = _owned(agent["id"])
    running = next((t for t in owned if t["schedule_kind"] == "card" and t.get("status") == "running"), None) \
        or next((t for t in owned if t["schedule_kind"] != "card" and t.get("run_started_at")), None)
    needs_you = len(agent_service.inbox(agent["id"])) + sum(
        1 for t in owned if t["schedule_kind"] == "card" and t.get("status") == "review")
    may_run, why_not = agent_service.can_run(agent["id"])
    status = ("off" if not agent["enabled"] else "working" if running else "needs_you" if needs_you
              else "capped" if not may_run else "idle")
    return {**agent, "status": status, "status_detail": running["name"] if running else why_not,
            "needs_you": needs_you, "runs_today": agent_service.runs_today(agent["id"])}


def _require(agent_id: str) -> dict:
    agent = agent_service.get(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")
    return agent


@router.get("")
async def list_agents(user: str = Depends(require_admin)) -> list[dict]:
    return [_summary(a) for a in agent_service.list_agents()]


@router.post("")
async def create_agent(body: AgentBody, user: str = Depends(require_admin)) -> dict:
    _check_endpoint(body.endpoint_id)
    try:
        fields = {k: v for k, v in body.model_dump().items() if v is not None and k != "enabled"}
        return _summary(agent_service.create(fields.pop("name", ""), **fields))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/inbox")
async def whole_inbox(user: str = Depends(require_admin)) -> dict:
    """Everything waiting on the person, across agents: open items, plus
    agents' results waiting for review (read from the cards, not copied)."""
    reviews = [t for t in task_service.list_tasks()
               if t.get("agent_id") and t["schedule_kind"] == "card" and t.get("status") == "review"]
    return {"items": agent_service.inbox(), "reviews": reviews,
            "count": len(agent_service.inbox()) + len(reviews)}


@router.get("/{agent_id}")
async def get_agent(agent_id: str, user: str = Depends(require_admin)) -> dict:
    agent = _require(agent_id)
    owned = _owned(agent_id)
    owned_ids = {t["id"] for t in owned}
    return {
        "agent": _summary(agent),
        "memory": agent_service.read_memory(agent_id),
        "goals": [t for t in owned if t["schedule_kind"] != "card"],
        "cards": [t for t in owned if t["schedule_kind"] == "card"],
        "inbox": agent_service.inbox(agent_id),
        "answered": agent_service.inbox(agent_id, status=None)[:20],
        "runs": [r for r in task_service.list_runs() if r["task_id"] in owned_ids][:50],
    }


@router.patch("/{agent_id}")
async def update_agent(agent_id: str, body: AgentBody, user: str = Depends(require_admin)) -> dict:
    _require(agent_id)
    fields = body.model_dump(exclude_unset=True)
    _check_endpoint(fields.get("endpoint_id"))
    try:
        return _summary(agent_service.update(agent_id, **fields))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/{agent_id}")
async def delete_agent(agent_id: str, user: str = Depends(require_admin)) -> dict:
    _require(agent_id)
    if any(t.get("status") == "running" for t in _owned(agent_id) if t["schedule_kind"] == "card"):
        raise HTTPException(status_code=409, detail="this agent is working on something; wait for it to finish")
    from core.session_manager import session_manager
    removed = task_service.delete_agent_work(agent_id)
    session_manager.release_agent_chats(agent_id)
    agent_service.delete(agent_id)
    return {"ok": True, "removed_work": removed}


@router.post("/{agent_id}/chat")
async def start_chat(agent_id: str, user: str = Depends(require_admin)) -> dict:
    """A chat with the agent: its model, and its identity and notes as they
    are now, frozen for this chat."""
    from core.session_manager import session_manager
    from services.agent_service import identity_block
    agent = _require(agent_id)
    endpoint_id = agent["endpoint_id"] or next(
        (e["id"] for e in model_endpoints.list_endpoints() if e["kind"] == "claude_cli"), None)
    if endpoint_id is None:
        raise HTTPException(status_code=400, detail="add a Claude connection in Settings, or pick this agent's model")
    session = session_manager.create_session(f"Chat with {agent['name']}")
    session_manager.set_model_endpoint(session["id"], endpoint_id)
    # Agents always run in Auto, chats with them included.
    session_manager.set_permission_mode(session["id"], "auto")
    return session_manager.set_agent(session["id"], agent_id,
                                     identity_block(agent, agent_service.read_memory(agent_id), chat=True))


@router.put("/{agent_id}/memory")
async def write_memory(agent_id: str, body: MemoryBody, user: str = Depends(require_admin)) -> dict:
    _require(agent_id)
    try:
        return {"memory": agent_service.write_memory(agent_id, body.text)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{agent_id}/goals")
async def add_goal(agent_id: str, body: GoalBody, user: str = Depends(require_admin)) -> dict:
    _require(agent_id)
    if body.schedule_kind not in ("daily", "interval"):
        raise HTTPException(status_code=400, detail="a goal runs daily or every so often")
    try:
        return task_service.create_task(body.name, body.prompt, body.schedule_kind, run_time=body.run_time,
                                        interval_seconds=body.interval_seconds, agent_id=agent_id,
                                        report_when=body.report_when)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{agent_id}/cards")
async def add_card(agent_id: str, body: CardBody, user: str = Depends(require_admin)) -> dict:
    _require(agent_id)
    try:
        return task_service.create_task(body.name, body.prompt, "card", status=body.status or "ready",
                                        agent_id=agent_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


def _continue(item: dict) -> str:
    """After an answer, let the work pick up again: a card waiting in Review
    goes back to Ready; a goal checks again now. Says what happened."""
    card = task_service.get_task(item["card_id"]) if item.get("card_id") else None
    if card is not None:
        if card.get("status") != "review":
            return "noted"
        if agent_service.open_items_for_card(card["id"]):
            return "noted; the card still waits on another answer"
        task_service.set_card_status(card["id"], "ready")
        return "the card will run again"
    goal = task_service.get_task(item["task_id"]) if item.get("task_id") else None
    if goal is not None:
        from core.task_scheduler import _run_task, agent_may_run
        if agent_may_run(goal):
            asyncio.get_running_loop().create_task(_run_task(goal))
            return "the goal is checking again now"
    return "noted"


@router.post("/inbox/{item_id}/answer")
async def answer(item_id: str, body: AnswerBody, user: str = Depends(require_admin)) -> dict:
    item = agent_service.get_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="inbox item not found")
    if item["status"] != "open":
        raise HTTPException(status_code=409, detail="this has already been answered")
    choice, text = body.choice, (body.text or "").strip()
    if choice not in ("reply", "dismiss"):
        raise HTTPException(status_code=400, detail="choose reply or dismiss")
    if choice == "reply" and not text:
        raise HTTPException(status_code=400, detail="write a reply first")
    if choice == "dismiss":
        agent_service.resolve(item_id, "dismissed")
        return {"ok": True, "next": "dismissed"}
    agent_service.resolve(item_id, "answered", text)
    if item["kind"] == "report":
        return {"ok": True, "next": "it will see your reply on its next run"}
    return {"ok": True, "next": _continue(item)}
