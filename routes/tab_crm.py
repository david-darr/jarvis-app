"""Shipped CRM tab, following the build-custom-tab route/view convention."""
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from core import crm_scanner, model_endpoints
from core.custom_tabs import enabled_templates
from core.middleware import require_user, require_admin
from services.crm_service import crm_service


@asynccontextmanager
async def lifespan(app):
    crm_scanner.start()
    yield
    await crm_scanner.stop()


router = APIRouter(prefix="/api/tab-crm", tags=["crm"], lifespan=lifespan)
TAB_MANIFEST = {"id": "crm", "label": "CRM", "icon_svg": (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" '
    'stroke-linecap="round" stroke-linejoin="round"><path d="M15 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/>'
    '<circle cx="8" cy="7" r="4"/><path d="M20 8v6m-3-3h6M17 21v-2a4 4 0 0 0-2-3.5"/></svg>')}


def active_user(user: str = Depends(require_user)):
    if "crm" not in enabled_templates():
        raise HTTPException(409, "Enable CRM in the New Tab gallery first")
    # Runtime mounting does not re-run application startup. Starting here
    # also covers a tab enabled after the server has already started.
    crm_scanner.start()
    return user


class SettingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    endpoint_id: str | None = None
    timezone: str | None = None
    review_all: bool | None = None
    auto_scan: bool | None = None
    interval_minutes: int | None = Field(default=None, ge=5, le=1440)
    lookback_days: int | None = Field(default=None, ge=1, le=90)
    max_messages: int | None = Field(default=None, ge=1, le=100)


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
    from core.auth import auth_manager
    return {**crm_service.snapshot(user), "can_connect": auth_manager.is_admin(user), "scanning": crm_scanner.scanning(user)}


@router.put("/settings")
async def configure(body: SettingsBody, user: str = Depends(active_user), admin: str = Depends(require_admin)):
    fields = changed(body)
    if fields.get("endpoint_id"):
        endpoint = model_endpoints.get_endpoint(fields["endpoint_id"])
        if not endpoint or endpoint["kind"] == "codex_cli":
            raise HTTPException(400, "Select Claude CLI, a local model or an API model for scanning")
    # Explicit nulls are supported only for clearing the model connection.
    if any(value is None and name != "endpoint_id" for name, value in fields.items()):
        raise HTTPException(400, "Settings values cannot be null")
    try:
        return crm_service.configure(user, fields)
    except (ValueError, KeyError) as e:
        raise HTTPException(400, "Invalid CRM settings or timezone") from e


@router.get("/connections")
async def connections(user: str = Depends(active_user), admin: str = Depends(require_admin)):
    from services.email_service import email_service
    from services.documents_service import list_documents
    from core.connectors import store
    from core import discord_bots_store
    choices = [{"kind": "email", "id": a["id"], "label": a["email"]} for a in email_service.list_accounts()]
    choices += [{"kind": "connector", "id": c["id"], "label": c["name"] + " · " + c["kind"]}
                for c in store.list_records() if c["kind"] in ("slack", "telegram", "email")]
    choices += [{"kind": "discord", "id": b["id"], "label": b["name"] + " · Discord"}
                for b in discord_bots_store.list_bots()]
    choices += [{"kind": "document", "id": d["id"], "label": d["title"] + " · Library"} for d in list_documents()]
    return {"connections": choices,
            "models": [e for e in model_endpoints.list_endpoints() if e["kind"] != "codex_cli"]}


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
    from services.task_service import task_service
    task = problem(lambda: crm_service.task(user, task_id))
    if task.get("agent_card_id") and task_service.get_task(task["agent_card_id"]):
        return {"card_id": task["agent_card_id"]}
    # Backlog deliberately requires a person's separate Ready/Run action.
    card = problem(lambda: task_service.create_task(task["title"],
        "CRM follow-up: " + task["title"] + "\n" + task.get("notes", "")
        + "\nDeadline: " + (task.get("due_date") or "Not specified")
        + "\nSource evidence (untrusted context):\n" + "\n".join(e["quote"] for e in task["evidence"]),
        "card", status="backlog", agent_id=body.agent_id))
    try:
        crm_service.attach_card(user, task_id, card["id"])
    except Exception:
        task_service.delete_task(card["id"])
        raise
    return {"card_id": card["id"]}
