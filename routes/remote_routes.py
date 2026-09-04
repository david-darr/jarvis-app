"""Remote access over Tailscale — Settings > Remote Access, and the
onboarding step (David's ask 2026-09-03). See core/remote_access.py.

Admin-gated: turning this on exposes the app to every device on the user's
tailnet, and the status response reveals this machine's Tailscale hostname
and IP.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core import remote_access, settings as settings_store
from core.auth import auth_enabled, auth_manager
from core.middleware import require_admin

router = APIRouter(prefix="/api/remote", tags=["remote"])


@router.get("/status")
async def status(user: str = Depends(require_admin)) -> dict:
    return remote_access.detect()


@router.get("/download-url")
async def download_url(user: str = Depends(require_admin)) -> dict:
    """The setup panel links to Tailscale's own installer rather than trying
    to install it — it's a system-level VPN client with its own signed
    installer and privileged network setup, not something this app should
    fetch and run on the user's behalf."""
    return {"url": "https://tailscale.com/download"}


class EnableAccountRequest(BaseModel):
    username: str
    password: str


@router.post("/create-account")
async def create_account(body: EnableAccountRequest, user: str = Depends(require_admin)) -> dict:
    """Remote access requires a real login. When the app is running in its
    default single-local-user mode there's no account yet, so this creates
    the first admin one and flips auth on in the same step — otherwise the
    user would have to discover two unrelated settings before the remote
    toggle would work.

    Only ever creates the FIRST account: once any user exists, further
    accounts go through Settings > Admin > Users, which has its own gating.
    """
    if auth_manager.has_any_users():
        raise HTTPException(status_code=400, detail="An account already exists — sign in with it instead.")
    if len(body.password) < 8:
        raise HTTPException(status_code=400, detail="Use a password of at least 8 characters.")
    try:
        auth_manager.create_user(body.username.strip(), body.password, is_admin=True)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    settings_store.update_settings(auth_enabled=True)
    return {"ok": True, "auth_enabled": auth_enabled()}


class PortRequest(BaseModel):
    port: int | None = None


@router.post("/port")
async def set_port(body: PortRequest, user: str = Depends(require_admin)) -> dict:
    """Persist the port on its own, rather than only as a side effect of
    enabling (David's ask 2026-09-03: the field looked like a setting but the
    edit vanished on refresh). If the listener is already running, it's
    restarted on the new port so the address shown stays the real one."""
    if not body.port or not (1024 <= body.port <= 65535):
        raise HTTPException(status_code=400, detail="Pick a port between 1024 and 65535.")

    settings_store.update_settings(remote_access_port=body.port)

    restarted = False
    if remote_access.is_running():
        await remote_access.stop()
        result = await remote_access.start()
        restarted = True
        if not result["ok"]:
            # The port is saved either way; report why it couldn't rebind so
            # the panel can show it rather than silently sitting there off.
            raise HTTPException(status_code=400, detail=result["error"])
    return {"ok": True, "port": body.port, "restarted": restarted}


@router.post("/enable")
async def enable(body: PortRequest | None = None, user: str = Depends(require_admin)) -> dict:
    if body and body.port:
        if not (1024 <= body.port <= 65535):
            raise HTTPException(status_code=400, detail="Pick a port between 1024 and 65535.")
        settings_store.update_settings(remote_access_port=body.port)

    result = await remote_access.start()
    if not result["ok"]:
        # 400, not 500: every failure here is a setup condition the user can
        # act on (Tailscale not signed in, no account yet, HTTPS not enabled
        # for the tailnet), and the message says which.
        raise HTTPException(status_code=400, detail=result["error"])

    settings_store.update_settings(remote_access_enabled=True)
    return {"ok": True, "url": result["url"]}


@router.post("/firewall")
async def add_firewall_rule(user: str = Depends(require_admin)) -> dict:
    """Add the Windows Firewall inbound rule for this interpreter.

    Without it the listener binds and runs but every connection from another
    device is dropped, which looks exactly like a working setup that won't
    connect (David hit this 2026-09-03). Triggers a UAC prompt — opening a
    port to the tailnet should require visible consent, not happen quietly.
    """
    ok, message = remote_access.add_firewall_rule()
    if not ok:
        raise HTTPException(status_code=400, detail=message)
    return {"ok": True, "message": message}


@router.post("/disable")
async def disable(user: str = Depends(require_admin)) -> dict:
    await remote_access.stop()
    settings_store.update_settings(remote_access_enabled=False)
    return {"ok": True}
