"""Authenticated Swarm UI API; there is no worker or test-dispatch endpoint."""
import asyncio
import json
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.middleware import require_user
from core.swarm import architect
from core.swarm.budget import BudgetLimit
from core.swarm.models import Conflict, NotFound, PersistenceFault

class PrivateRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def private(request):
            response = await handler(request)
            response.headers["Cache-Control"] = "no-store"
            return response

        return private


router = APIRouter(prefix="/api/swarm", tags=["swarm"], route_class=PrivateRoute)
ID = Annotated[str, Field(min_length=1, max_length=100)]
Name = Annotated[str, Field(min_length=1, max_length=120)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Limit(StrictModel):
    ceiling: int | None = Field(default=None, strict=True, ge=2, le=1_000_000_000)
    pause_percent: int = Field(default=80, strict=True, ge=1, le=100)
    checkpoint_reserve: int = Field(default=0, strict=True, ge=0, le=1_000_000_000)

    @model_validator(mode="after")
    def valid_limit(self):
        BudgetLimit(**self.model_dump())
        return self


class Member(StrictModel):
    id: ID | None = None
    name: Name
    role: Name
    instructions: str = Field(default="", max_length=10000)
    limit: Limit
    # Which saved model connection this teammate runs on. The account identity
    # that decides its shared allowance is derived from the connection by the
    # service and can never be supplied here.
    endpoint_id: ID | None = None
    model: Annotated[str, Field(max_length=200)] | None = None
    effort: Annotated[str, Field(max_length=40)] | None = None
    step_limit: int | None = Field(default=None, strict=True, ge=500, le=1_000_000_000)


class ShiftSchedule(StrictModel):
    enabled: bool = Field(default=False, strict=True)
    days: list[int] = Field(default_factory=list, max_length=7)
    start_time: str = Field(default="09:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    end_time: str = Field(default="17:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    max_cycles: int = Field(default=5, strict=True, ge=1, le=50)
    auto_spend_confirmed: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def valid_schedule(self):
        if len(self.days) != len(set(self.days)) or any(day < 0 or day > 6 for day in self.days):
            raise ValueError("Shift days must be unique values from 0 through 6")
        if self.enabled and not self.days:
            raise ValueError("Choose at least one shift day")
        if self.enabled and self.start_time == self.end_time:
            raise ValueError("Shift start and end times must differ")
        if self.enabled and not self.auto_spend_confirmed:
            raise ValueError("Confirm scheduled provider spending before enabling shifts")
        return self


class MemorySettings(StrictModel):
    sources: list[Literal["vault", "sessions", "library", "project"]] = Field(default_factory=list, max_length=4)
    project_id: ID | None = None
    max_results: int = Field(default=5, strict=True, ge=1, le=5)

    @model_validator(mode="after")
    def valid_memory(self):
        if len(self.sources) != len(set(self.sources)):
            raise ValueError("Memory sources must be unique")
        if "project" in self.sources and not self.project_id:
            raise ValueError("Choose a JARVIS Project before enabling project memory")
        return self


class Setup(StrictModel):
    name: Name
    mission: str = Field(min_length=1, max_length=10000)
    mode: Literal["guided", "autonomous", "scheduled"] = "guided"
    system_limit: Limit
    run_limit: Limit
    pool_limit: Limit
    pool_id: ID | None = None
    lead: Member
    specialists: list[Member] = Field(default_factory=list, max_length=20)
    schedule: ShiftSchedule = Field(default_factory=ShiftSchedule)
    memory: MemorySettings = Field(default_factory=MemorySettings)

    @model_validator(mode="after")
    def valid_mode(self):
        if (self.mode == "scheduled") != self.schedule.enabled:
            raise ValueError("Scheduled mode and its shift schedule must be enabled together")
        return self


class Create(Setup):
    command_id: ID

    @model_validator(mode="after")
    def new_agents(self):
        if any(member.id for member in [self.lead, *self.specialists]):
            raise ValueError("New systems cannot reference existing agents")
        return self


class PoolLimit(StrictModel):
    id: ID
    limit: Limit


class Update(Setup):
    command_id: ID
    expected_revision: int = Field(strict=True, ge=0)
    pool_limits: list[PoolLimit] = Field(default_factory=list, max_length=21)


class Command(StrictModel):
    command_id: ID


class Lifecycle(Command):
    expected_revision: int = Field(strict=True, ge=0)
    spend_confirmed: bool = Field(default=False, strict=True)


class DeleteSystem(Command):
    expected_revision: int = Field(strict=True, ge=0)
    confirmation: Name


class DraftTeam(StrictModel):
    description: str = Field(min_length=1, max_length=4000)
    endpoint_id: ID


class Reconcile(Command):
    evidence: str = Field(min_length=1, max_length=4000)


class Message(Command):
    body: str = Field(min_length=1, max_length=20000)


def human(request: Request):
    owner = require_user(request)
    if owner == "internal-tool":
        raise HTTPException(403, "A human account is required")
    return owner


def service(request: Request):
    value = getattr(request.app.state, "swarm", None)
    if value is None:
        raise HTTPException(503, "Swarm is unavailable")
    return value


def domain(call):
    try:
        return call()
    except architect.DraftFailed as exc:
        raise HTTPException(422, str(exc))
    except NotFound:
        raise HTTPException(404, "System or resource not found")
    except Conflict as exc:
        raise HTTPException(409, str(exc))
    except PersistenceFault:
        raise HTTPException(503, "Swarm persistence is unavailable")
    except ValueError as exc:
        raise HTTPException(422, str(exc))


async def mutation(operation):
    try:
        return await operation
    except (NotFound, Conflict, PersistenceFault, ValueError, architect.DraftFailed) as exc:
        def raise_error():
            raise exc
        return domain(raise_error)


def ready(service):
    domain(service.ready)
    return service.store


@router.get("/status")
async def status(owner=Depends(human), svc=Depends(service)):
    return svc.status()


@router.get("/pools")
async def pools(owner=Depends(human), svc=Depends(service)):
    return domain(lambda: ready(svc).owner_pools(owner))


@router.get("/systems")
async def systems(offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200), owner=Depends(human), svc=Depends(service)):
    return domain(lambda: ready(svc).owner_systems(owner, offset, limit))


@router.post("/systems", status_code=201)
async def create(body: Create, owner=Depends(human), svc=Depends(service)):
    return await mutation(svc.create(owner, body.model_dump(exclude={"command_id"}), body.command_id))


@router.get("/systems/{system_id}")
async def snapshot(system_id: str, owner=Depends(human), svc=Depends(service)):
    result = domain(lambda: ready(svc).owner_snapshot(owner, system_id))
    # Ownership is established by the line above, so this company's own
    # capability verdict can be included without revealing anyone else's.
    return {**result, "availability": svc.status(system_id)}


@router.patch("/systems/{system_id}")
async def update(system_id: str, body: Update, owner=Depends(human), svc=Depends(service)):
    return await mutation(svc.update(owner, system_id, body.model_dump(exclude={"command_id", "expected_revision"}), body.command_id, body.expected_revision))


@router.delete("/systems/{system_id}")
async def delete(system_id: str, body: DeleteSystem, owner=Depends(human), svc=Depends(service)):
    return await mutation(svc.delete(owner, system_id, body.confirmation, body.command_id, body.expected_revision))


@router.post("/systems/{system_id}/messages", status_code=202)
async def message(system_id: str, body: Message, owner=Depends(human), svc=Depends(service)):
    return await mutation(svc.message(owner, system_id, body.body, body.command_id))


@router.post("/draft-team")
async def draft_team(body: DraftTeam, owner=Depends(human), svc=Depends(service)):
    """Spends tokens on the named connection, and only when asked. Creates
    nothing: the reply is a proposal for the setup form."""
    return await mutation(svc.draft_team(owner, body.description, body.endpoint_id))


@router.post("/systems/{system_id}/attempts/{attempt_id}/reconcile")
async def reconcile(system_id: str, attempt_id: str, body: Reconcile, owner=Depends(human), svc=Depends(service)):
    return await mutation(svc.reconcile(owner, system_id, attempt_id, body.evidence))


@router.post("/systems/{system_id}/checkpoints", status_code=201)
async def checkpoint(system_id: str, body: Command, owner=Depends(human), svc=Depends(service)):
    return await mutation(svc.checkpoint(owner, system_id, body.command_id))


@router.get("/systems/{system_id}/checkpoints/{checkpoint_id}/download")
async def download(system_id: str, checkpoint_id: str, format: Literal["md", "json"] = "md", owner=Depends(human), svc=Depends(service)):
    content, media_type = domain(lambda: svc.download(owner, system_id, checkpoint_id, format))
    # Never put user-controlled names/paths into headers or open a client path.
    return Response(content, media_type=media_type, headers={"Content-Disposition": f'attachment; filename="swarm-handoff.{format}"', "Cache-Control": "no-store"})


@router.get("/systems/{system_id}/events")
async def events(system_id: str, request: Request, after: int = Query(0, ge=0), owner=Depends(human), svc=Depends(service)):
    store = ready(svc)
    domain(lambda: store.get_system(system_id, owner=owner))
    try:
        cursor = max(after, int(request.headers.get("last-event-id", "0")))
    except ValueError:
        raise HTTPException(422, "Invalid event cursor")

    async def stream():
        nonlocal cursor
        while not await request.is_disconnected():
            try:
                # Cookie expiry/revocation must stop an already-open stream.
                if human(request) != owner:
                    return
                ready(svc)
                rows = store.owner_events(owner, system_id, cursor)
                for row in rows:
                    if human(request) != owner:
                        return
                    cursor = row["id"]
                    yield f"id: {cursor}\nevent: change\ndata: {json.dumps(row, ensure_ascii=False)}\n\n"
                if not rows:
                    yield ": keepalive\n\n"
                await asyncio.sleep(0.5 if len(rows) < 100 else 0)
            except (HTTPException, NotFound, PersistenceFault):
                yield 'event: unavailable\ndata: {}\n\n'
                return

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.get("/systems/{system_id}/{collection}")
async def page(system_id: str, collection: Literal["tasks", "messages", "runs", "attempts", "activity", "checkpoints", "shifts"],
               offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200), owner=Depends(human), svc=Depends(service)):
    collection = "events" if collection == "activity" else collection
    return domain(lambda: ready(svc).owner_page(owner, system_id, collection, offset, limit))


@router.post("/systems/{system_id}/{action}")
async def lifecycle(system_id: str, action: Literal["start", "pause", "stop", "resume", "archive", "restore"],
                    body: Lifecycle, owner=Depends(human), svc=Depends(service)):
    result = await mutation(svc.lifecycle(owner, system_id, action, body.command_id, body.expected_revision,
                                          spend_confirmed=body.spend_confirmed))
    if result["status"] == "blocked":
        return JSONResponse({**result, "detail": result["detail"]}, status_code=409)
    return result
