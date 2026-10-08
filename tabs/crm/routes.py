"""Shipped CRM folder tab, using the stable core.tab_api interface."""
import asyncio
import logging
import time
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from core import tab_api
from core.tab_api import require_user, require_admin
from . import scanner as crm_scanner
from . import work as crm_work
from .service import crm_service

api = tab_api.for_tab(__package__)
logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/tab-crm", tags=["crm"])


def active_user(user: str = Depends(require_user)):
    if not api.is_on:
        raise HTTPException(409, "Add CRM in the Tool Store first")
    return user


class SettingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    endpoint_id: str | None = None
    model: str | None = None
    timezone: str | None = None
    review_all: bool | None = None
    auto_scan: bool | None = None
    interval_minutes: int | None = Field(default=None, ge=5, le=1440)
    lookback_days: int | None = Field(default=None, ge=1, le=90)
    max_messages: int | None = Field(default=None, ge=1, le=100)
    schedule_mode: Literal["interval", "times"] | None = None
    schedule_times: list[str] | None = Field(default=None, min_length=1, max_length=6)
    schedule_days: list[int] | None = Field(default=None, min_length=1, max_length=7)


class SourceBody(BaseModel):
    kind: Literal["email", "connector", "discord", "document"]
    connection_id: str
    folder: str = "INBOX"
    conversations: list[str] = Field(default_factory=list, max_length=100)


class ToggleBody(BaseModel):
    enabled: bool


class TaskBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, min_length=1, max_length=300)
    notes: str | None = Field(default=None, max_length=10000)
    contact: str | None = Field(default=None, max_length=300)
    project: str | None = Field(default=None, max_length=150)
    due_date: str | None = None
    priority: Literal["urgent", "high", "normal", "low"] | None = None
    status: Literal["active", "in_progress", "waiting", "needs_review", "done", "dismissed"] | None = None
    snoozed_until: str | None = None


class ReviewBody(BaseModel):
    accept: bool


class ScanBody(BaseModel):
    retry: bool = False


class AgentBody(BaseModel):
    agent_id: str


def changed(body):
    return body.model_dump(exclude_unset=True)


def problem(call):
    try:
        return call()
    except KeyError:
        raise HTTPException(404, "CRM record not found")
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e))


@router.get("")
async def overview(user: str = Depends(active_user)):
    return {**crm_service.snapshot(user), "can_connect": tab_api.is_admin(user), "scanning": crm_scanner.scanning(user)}


@router.put("/settings")
async def configure(body: SettingsBody, user: str = Depends(active_user), admin: str = Depends(require_admin)):
    fields = changed(body)
    if fields.get("endpoint_id"):
        endpoint = next((e for e in tab_api.list_models() if e["id"] == fields["endpoint_id"]), None)
        if not endpoint or endpoint["kind"] == "codex_cli":
            raise HTTPException(400, "Select Claude CLI, a local model or an API model for scanning")
    if any(value is None and name not in ("endpoint_id", "model") for name, value in fields.items()):
        raise HTTPException(400, "Settings values cannot be null")
    previous = crm_service.settings(user)
    endpoint_id = fields.get("endpoint_id", previous["endpoint_id"])
    selected = fields.get("model", previous.get("model"))
    if selected is not None:
        endpoint = next((e for e in tab_api.list_models() if e["id"] == endpoint_id), None)
        offered = await tab_api.model_choices(endpoint_id) if endpoint else []
        valid = selected in {m["id"] for m in offered}
        if endpoint and endpoint["kind"] in ("local", "api"):
            valid = selected == endpoint.get("model")
        if not valid:
            if endpoint_id != previous["endpoint_id"]:
                fields["model"] = None
            else:
                raise HTTPException(400, "Choose a model this connection offers")
    try:
        return crm_service.configure(user, fields)
    except (ValueError, KeyError) as e:
        raise HTTPException(400, "Invalid CRM settings, schedule or timezone") from e


@router.get("/models/{endpoint_id}")
async def models(endpoint_id: str, user: str = Depends(active_user)):
    try:
        return await tab_api.model_choices(endpoint_id)
    except ValueError as e:
        raise HTTPException(404, "Model connection not found") from e


@router.get("/connections")
async def connections(user: str = Depends(active_user), admin: str = Depends(require_admin)):
    choices = [{"kind": "email", "id": a["id"], "label": a["email"]} for a in tab_api.email_accounts()]
    choices += tab_api.message_connections()
    choices += [{"kind": "document", "id": d["id"], "label": d["title"] + " ? Library"} for d in tab_api.documents()]
    return {"connections": choices,
            "models": [e for e in tab_api.list_models() if e["kind"] != "codex_cli"]}


@router.post("/sources")
async def add_source(body: SourceBody, user: str = Depends(active_user), admin: str = Depends(require_admin)):
    allowed = await connections(user, admin)
    found = next((c for c in allowed["connections"] if c["kind"] == body.kind and c["id"] == body.connection_id), None)
    if not found:
        raise HTTPException(400, "Select an available connection")
    # Creates the owner configuration too, so connector capture can find it.
    crm_service.configure(user, {})
    return problem(lambda: crm_service.add_source(user, body.kind, body.connection_id, found["label"],
                                                 body.folder, body.conversations))


@router.patch("/sources/{source_id}")
async def toggle_source(source_id: str, body: ToggleBody, user: str = Depends(active_user)):
    return problem(lambda: crm_service.toggle_source(user, source_id, body.enabled))


@router.post("/scan")
async def run_scan(body: ScanBody, user: str = Depends(active_user), admin: str = Depends(require_admin)):
    try:
        return crm_scanner.begin_scan(user, body.retry)
    except ValueError as e:
        raise HTTPException(409, str(e))


@router.post("/tasks")
async def add_task(body: TaskBody, user: str = Depends(active_user)):
    fields = changed(body)
    if any(value is None and name not in ("due_date", "snoozed_until") for name, value in fields.items()):
        raise HTTPException(400, "Only deadlines and snoozes can be cleared with null")
    return problem(lambda: crm_service.create_task(user, fields))


@router.patch("/tasks/{task_id}")
async def edit_task(task_id: str, body: TaskBody, user: str = Depends(active_user)):
    fields = changed(body)
    if any(value is None and name not in ("due_date", "snoozed_until") for name, value in fields.items()):
        raise HTTPException(400, "Only deadlines and snoozes can be cleared with null")
    return problem(lambda: crm_service.update_task(user, task_id, fields))


@router.post("/tasks/{task_id}/review")
async def review(task_id: str, body: ReviewBody, user: str = Depends(active_user)):
    return problem(lambda: crm_service.resolve_proposal(user, task_id, body.accept))


@router.get("/messages/{message_id}")
async def evidence(message_id: str, user: str = Depends(active_user)):
    return problem(lambda: crm_service.message(user, message_id))


@router.post("/tasks/{task_id}/agent")
async def assign(task_id: str, body: AgentBody, user: str = Depends(active_user), admin: str = Depends(require_admin)):
    task = problem(lambda: crm_service.task(user, task_id))
    if task.get("agent_card_id") and tab_api.backlog_card(task["agent_card_id"]):
        return {"card_id": task["agent_card_id"]}
    # Backlog deliberately requires a person's separate Ready/Run action.
    card = problem(lambda: tab_api.create_backlog_card(task["title"],
        "CRM follow-up: " + task["title"] + "\n" + task.get("notes", "")
        + "\nDeadline: " + (task.get("due_date") or "Not specified")
        + "\nSource evidence (untrusted context):\n" + "\n".join(e["quote"] for e in task["evidence"]),
        agent_id=body.agent_id))
    try:
        crm_service.attach_card(user, task_id, card["id"])
    except Exception:
        tab_api.delete_backlog_card(card["id"])
        raise
    return {"card_id": card["id"]}


class ReplyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    draft: str = Field(max_length=20000)


def reply_state(user, task_id):
    task = problem(lambda: crm_service.task(user, task_id))
    target = crm_service.reply_target(user, task_id)
    return {"draft": task.get("reply_draft") or "", "last_reply": task.get("last_reply"),
            # Sending is an admin's explicit action; others can still copy.
            "target": target and {k: target[k] for k in ("to", "subject", "account")},
            "can_send": bool(target) and tab_api.is_admin(user)}


@router.get("/tasks/{task_id}/reply")
async def get_reply(task_id: str, user: str = Depends(active_user)):
    return reply_state(user, task_id)


@router.post("/tasks/{task_id}/draft")
async def draft(task_id: str, user: str = Depends(active_user)):
    problem(lambda: crm_service.task(user, task_id))
    try:
        await crm_work.draft_reply(user, task_id)
    except (ValueError, asyncio.TimeoutError) as e:
        raise HTTPException(400, str(e) if isinstance(e, ValueError) else "Drafting timed out; try again")
    except Exception as e:
        # Never echo provider error text: it can include request URLs/keys.
        logger.warning("CRM draft failed: %s", type(e).__name__)
        raise HTTPException(502, "Drafting failed; check the model in Sources and try again")
    return reply_state(user, task_id)


@router.put("/tasks/{task_id}/reply")
async def save_reply(task_id: str, body: ReplyBody, user: str = Depends(active_user)):
    problem(lambda: crm_service.set_work(user, task_id, reply_draft=body.draft))
    return reply_state(user, task_id)


@router.post("/tasks/{task_id}/send")
async def send_reply(task_id: str, body: ReplyBody, user: str = Depends(active_user), admin: str = Depends(require_admin)):
    problem(lambda: crm_service.task(user, task_id))
    # The recipient, account and thread come from the stored source message,
    # never from the request, so a page can't redirect the reply.
    target = crm_service.reply_target(user, task_id)
    if not target:
        raise HTTPException(400, "This task has no email to reply to")
    try:
        await asyncio.to_thread(tab_api.send_email, user, target["account_id"], target["to"],
                                target["subject"], body.draft, target["in_reply_to"])
    except (ValueError, PermissionError) as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.warning("CRM reply failed to send: %s", type(e).__name__)
        raise HTTPException(502, "The email couldn't be sent; check the account in Email settings")
    crm_service.set_work(user, task_id, reply_draft="",
                         last_reply={"at": time.time(), "to": target["to"], "subject": target["subject"]})
    return reply_state(user, task_id)


@router.post("/tasks/{task_id}/chat")
async def task_chat(task_id: str, user: str = Depends(active_user)):
    return {"session_id": problem(lambda: crm_work.open_chat(user, task_id))}


class CompletedBody(BaseModel):
    completed: bool


@router.patch("/tasks/{task_id}/completed")
async def complete_task(task_id: str, body: CompletedBody, user: str = Depends(active_user)):
    return problem(lambda: crm_service.update_task(user, task_id,
        {"status": "done" if body.completed else "active"}))
