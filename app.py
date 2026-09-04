# app.py — slim orchestrator (Odysseus-style: wiring only, no business logic here)
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from core.constants import STATIC_DIR
from core.middleware import SecurityHeadersMiddleware
from core import custom_tabs, remote_access, task_scheduler, vault_sync
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
from services import chat_service, skills_service

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

# Also log to a file in the data directory. In a packaged install the backend
# is a child process of Electron and its output goes to Electron's own stdout
# — i.e. nowhere a user or a support session can see it. Diagnosing anything
# meant reproducing it outside the app (which is exactly what the 2026-09-03
# remote-access investigation had to do). Rotating, capped, and in the data
# directory so it survives updates and never grows unbounded.
try:
    from logging.handlers import RotatingFileHandler
    from core.constants import DATA_DIR

    _log_dir = os.path.join(DATA_DIR, "logs")
    os.makedirs(_log_dir, exist_ok=True)
    _file_handler = RotatingFileHandler(
        os.path.join(_log_dir, "backend.log"), maxBytes=2_000_000, backupCount=3, encoding="utf-8",
    )
    _file_handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
    logging.getLogger().addHandler(_file_handler)
except Exception:  # never let logging setup stop the app from booting
    logging.getLogger(__name__).exception("couldn't set up file logging")


class _ProactorResetNoiseFilter(logging.Filter):
    """Drop a specific, non-actionable asyncio traceback on Windows.

    Every local HTTP call (Ollama polling especially) tears down its
    connection afterwards, and Windows' Proactor event loop raises
    ConnectionResetError [WinError 10054] inside
    _ProactorBasePipeTransport._call_connection_lost while doing so. The
    request itself has already succeeded — this fires purely on cleanup —
    but asyncio logs it at ERROR with a full traceback, dozens of times a
    session. David spotted it in the logs and reasonably read it as a real
    fault (2026-09-03); worse, the volume buries errors that do matter.

    Scoped as tightly as possible so it can never hide a genuine problem:
    only records from the asyncio logger, only when the exception really is
    a ConnectionResetError, and only from that one transport callback.
    Anything else — including any other ConnectionResetError — still logs.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != "asyncio":
            return True
        exc = record.exc_info[1] if record.exc_info else None
        if not isinstance(exc, ConnectionResetError):
            return True
        return "_call_connection_lost" not in str(record.getMessage())


logging.getLogger("asyncio").addFilter(_ProactorResetNoiseFilter())


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Replaces the deprecated @app.on_event("startup"/"shutdown") pair —
    # FastAPI's own docs mark those deprecated in favor of this single
    # context manager; identical behavior, one modern mechanism.
    # Vault <-> Notes reconciliation before anything reads Notes (David's ask
    # 2026-09-03: the app reported "no priorities" while the connected vault
    # was full of them). See core/vault_sync.py.
    vault_sync.sync_on_startup()
    # Bundled skills (build-custom-tab, humanizer) copied into the user's
    # skills folder on first run — data/ isn't packaged, so they can only
    # arrive from skill_templates/. Per-slug once-only; see the function.
    skills_service.seed_default_skills()
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
# Tabs the user builds live in the data directory so app updates can't wipe
# them (David's ask 2026-09-03). migrate_user_tabs() relocates any created by
# an older build, before discovery runs.
custom_tabs.migrate_user_tabs()
custom_tabs.mount_all(app)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
# User-built tab views, served from the data directory. Same-origin, so the
# `script-src 'self'` CSP in core/middleware.py covers the dynamic import().
app.mount(custom_tabs.USER_VIEWS_URL, StaticFiles(directory=custom_tabs.USER_VIEWS_DIR), name="custom-views")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(f"{STATIC_DIR}/index.html")


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok"}


