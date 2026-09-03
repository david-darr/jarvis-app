# app.py — slim orchestrator (Odysseus-style: wiring only, no business logic here)
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from core.constants import STATIC_DIR
from core.middleware import SecurityHeadersMiddleware
from core import custom_tabs, remote_access, task_scheduler
from core.channels import discord_channel
from routes import (
    auth_routes,
    chat_routes,
    session_routes,
    skills_routes,
    notes_routes,
    task_routes,
    calendar_routes,
    email_routes,
    settings_routes,
    model_routes,
    workspace_routes,
    system_routes,
    integrations_routes,
    channels_routes,
    document_routes,
    vault_routes,
    cookbook_routes,
    remote_routes,
)
from core import llamacpp_engine
from services import chat_service

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Replaces the deprecated @app.on_event("startup"/"shutdown") pair —
    # FastAPI's own docs mark those deprecated in favor of this single
    # context manager; identical behavior, one modern mechanism.
    task_scheduler.start()
    await discord_channel.start()
    # Restores the remote listener across restarts if the user turned it on
    # (core/remote_access.py). Never raises — a machine that's dropped off
    # the tailnet must still boot normally.
    await remote_access.start_if_enabled()
    yield
    await remote_access.stop()
    task_scheduler.stop()
    await discord_channel.stop()
    await chat_service.shutdown()
    # Built-in local engine (David's ask 2026-09-01) runs as a real
    # subprocess — never leave it orphaned on app shutdown, same reasoning
    # as electron/main.js's own stopBackend().
    llamacpp_engine.stop()


app = FastAPI(title="JARVIS", lifespan=lifespan)

app.add_middleware(SecurityHeadersMiddleware)

app.include_router(auth_routes.router)
app.include_router(session_routes.router)
app.include_router(chat_routes.router)
app.include_router(skills_routes.router)
app.include_router(notes_routes.router)
app.include_router(task_routes.router)
app.include_router(calendar_routes.router)
app.include_router(email_routes.router)
app.include_router(settings_routes.router)
app.include_router(model_routes.router)
app.include_router(workspace_routes.router)
app.include_router(system_routes.router)
app.include_router(integrations_routes.router)
app.include_router(channels_routes.router)
app.include_router(document_routes.router)
app.include_router(vault_routes.router)
app.include_router(cookbook_routes.router)
app.include_router(remote_routes.router)

# Developer Mode (David's ask 2026-09-01) — the only app.py edit a custom
# tab ever needs. Every routes/tab_*.py found here gets mounted; adding a
# new tab afterward is purely new files, see core/custom_tabs.py.
custom_tabs.mount_all(app)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(f"{STATIC_DIR}/index.html")


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok"}


