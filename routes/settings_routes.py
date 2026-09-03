"""Settings — the Settings tab. Vault location, Discord channel config, and
onboarding state. All admin-gated (matches Odysseus's own settings-write
policy — see specs/auth-security.md): in the single-user default this is a
no-op (SINGLE_USER is always admin), it only matters once AUTH_ENABLED=true.
"""
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from core import discord_bots_store, settings as settings_store
from core.channels import discord_channel
from core.middleware import require_admin
from core.session_manager import session_manager
from core.vault import build_custom_vault, resolve_vault_dir, seed_vault_if_empty

router = APIRouter(prefix="/api/settings", tags=["settings"])


class VaultDirRequest(BaseModel):
    path: str


class CreateDiscordBotRequest(BaseModel):
    name: str
    token: str
    allowed_user_id: Optional[str] = None
    model_endpoint_id: Optional[str] = None


class UpdateDiscordBotRequest(BaseModel):
    name: str
    token: Optional[str] = None  # blank/omitted = keep the existing token
    allowed_user_id: Optional[str] = None
    model_endpoint_id: Optional[str] = None


class VaultSetupRequest(BaseModel):
    areas: list[str] = []
    profile_note: Optional[str] = None


# Well-known Claude Agent SDK / Claude Code builtin tool names. The SDK
# doesn't export a canonical enum of these, so this list is hand-maintained —
# same trade-off Odysseus takes with its own builtin-tool-toggle list.
AGENT_TOOLS = ["Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebFetch", "WebSearch", "Task", "TodoWrite", "NotebookEdit"]


class SetDisabledToolsRequest(BaseModel):
    disabled_tools: list[str]


@router.get("")
async def get_settings(user: str = Depends(require_admin)) -> dict:
    raw = settings_store.load_settings()
    return {
        "onboarding_complete": raw["onboarding_complete"],
        "vault_dir": raw["vault_dir"] or resolve_vault_dir(),
        "disabled_tools": raw["disabled_tools"],
        "developer_mode_enabled": raw["developer_mode_enabled"],
    }


class SetDeveloperModeRequest(BaseModel):
    enabled: bool


@router.post("/developer-mode")
async def set_developer_mode(body: SetDeveloperModeRequest, user: str = Depends(require_admin)) -> dict:
    """Sidebar toggle (David's ask 2026-09-01) — flips the theme to red and
    is the entry point for building custom tabs (see core/custom_tabs.py).
    Purely cosmetic/contextual: it does not gate whether already-built
    custom tabs show up in the nav, only the theme."""
    settings_store.update_settings(developer_mode_enabled=body.enabled)
    return {"ok": True}


@router.post("/onboarding-complete")
async def complete_onboarding(user: str = Depends(require_admin)) -> dict:
    settings_store.update_settings(onboarding_complete=True)
    return {"ok": True}


@router.post("/vault-dir")
async def set_vault_dir(body: VaultDirRequest, user: str = Depends(require_admin)) -> dict:
    settings_store.update_settings(vault_dir=body.path)
    seed_vault_if_empty(body.path)
    # Only affects *new* Brain connections (new sessions, or existing
    # sessions after their next reconnect) — a session with an already-open
    # Claude Agent SDK connection keeps its original cwd until it reconnects.
    # Known limitation, not silently hidden: real "switch vault live" needs
    # tearing down every open Brain, which isn't built this pass.
    return {"ok": True, "note": "Existing open chat sessions keep their old vault until reconnected; new sessions use the new path immediately."}


@router.post("/vault-setup")
async def setup_vault(body: VaultSetupRequest, user: str = Depends(require_admin)) -> dict:
    """Onboarding questionnaire (David's ask 2026-08-31) — builds a vault
    structure shaped to the areas the user said they want tracked, replacing
    the generic seed if nothing real has touched it yet (see
    core/vault.py::build_custom_vault's untouched-seed guard)."""
    vault_dir = settings_store.get_setting("vault_dir") or resolve_vault_dir()
    applied = build_custom_vault(vault_dir, body.areas, body.profile_note or "")
    return {"ok": True, "vault_dir": vault_dir, "applied": applied}


@router.get("/discord-bots")
async def list_discord_bots(user: str = Depends(require_admin)) -> list[dict]:
    """David's ask 2026-09-01: "you should be able to see all the bots you
    have connected to" — multiple bots, not the original single-token
    design (see core/discord_bots_store.py)."""
    return discord_bots_store.list_bots()


@router.post("/discord-bots")
async def create_discord_bot(body: CreateDiscordBotRequest, user: str = Depends(require_admin)) -> dict:
    bot = discord_bots_store.create_bot(body.name, body.token, body.allowed_user_id, body.model_endpoint_id)
    await discord_channel.restart()
    return bot


@router.patch("/discord-bots/{bot_id}")
async def update_discord_bot(bot_id: str, body: UpdateDiscordBotRequest, user: str = Depends(require_admin)) -> dict:
    try:
        bot = discord_bots_store.update_bot(
            bot_id, name=body.name, token=body.token,
            allowed_user_id=body.allowed_user_id, model_endpoint_id=body.model_endpoint_id,
        )
    except KeyError:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="bot not found")
    # Real gap found live, 2026-09-01: get_or_create_channel_session() only
    # ever applies model_endpoint_id when a channel session is first
    # created — an already-existing conversation (exactly what David hit:
    # he'd already messaged the bot before a model was ever configured)
    # kept no model forever, silently ignoring this exact Settings change.
    # Push it onto the existing pinned session too, not just future ones.
    existing_session_id = session_manager.get_channel_session_id(f"discord:{bot_id}")
    if existing_session_id:
        session_manager.set_model_endpoint(existing_session_id, body.model_endpoint_id)
    await discord_channel.restart()
    return bot


@router.delete("/discord-bots/{bot_id}")
async def delete_discord_bot(bot_id: str, user: str = Depends(require_admin)) -> dict:
    discord_bots_store.delete_bot(bot_id)
    await discord_channel.restart()
    return {"ok": True}


@router.get("/agent-tools")
async def list_agent_tools(user: str = Depends(require_admin)) -> dict:
    disabled = settings_store.get_setting("disabled_tools") or []
    return {"available": AGENT_TOOLS, "disabled": disabled}


@router.post("/agent-tools")
async def set_disabled_tools(body: SetDisabledToolsRequest, user: str = Depends(require_admin)) -> dict:
    """Globally disables the listed tools for every new Brain connection
    (see core/brain.py's disallowed_tools wiring) — takes effect on the next
    new/reconnected session, same "not live for already-open sessions"
    caveat as vault-dir above."""
    unknown = set(body.disabled_tools) - set(AGENT_TOOLS)
    if unknown:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=f"unknown tool(s): {', '.join(unknown)}")
    settings_store.update_settings(disabled_tools=body.disabled_tools)
    return {"ok": True}
