"""Auth HTTP surface: status, first-run setup, login/logout, TOTP enrollment.

Only relevant when AUTH_ENABLED=true — see core/auth.py for the single-user
default that skips all of this.
"""
from fastapi import APIRouter, Depends, HTTPException, Response, Request
from pydantic import BaseModel

from core.auth import auth_manager, auth_enabled, SESSION_COOKIE_NAME, SESSION_TTL_SECONDS
from core.middleware import get_current_user, require_admin, require_user

router = APIRouter(prefix="/api/auth", tags=["auth"])


class SetupRequest(BaseModel):
    username: str
    password: str


class LoginRequest(BaseModel):
    username: str
    password: str
    totp_code: str | None = None


@router.get("/status")
async def status(request: Request) -> dict:
    user = get_current_user(request)
    return {
        "auth_enabled": auth_enabled(),
        "setup_required": auth_enabled() and not auth_manager.has_any_users(),
        "username": user,
        "is_admin": auth_manager.is_admin(user) if user else False,
        # Whether a real "Switch User" is even meaningful right now (David's
        # ask 2026-08-31) — a plain boolean, not the user list itself:
        # GET /users is admin-only (enumerating accounts is a real privacy
        # leak on a shared machine), but "does more than one account exist"
        # is safe to expose to anyone so the sidebar can gate the option.
        "other_users_exist": auth_enabled() and len(auth_manager.list_users()) > 1,
    }


@router.post("/setup")
async def setup(body: SetupRequest, response: Response) -> dict:
    if not auth_enabled():
        raise HTTPException(status_code=400, detail="AUTH_ENABLED is false — no setup needed")
    if auth_manager.has_any_users():
        raise HTTPException(status_code=400, detail="setup already completed")
    auth_manager.create_user(body.username, body.password, is_admin=True)
    token = auth_manager.create_session(body.username)
    response.set_cookie(SESSION_COOKIE_NAME, token, httponly=True, samesite="lax", max_age=SESSION_TTL_SECONDS)
    return {"username": body.username, "is_admin": True}


@router.post("/login")
async def login(body: LoginRequest, response: Response) -> dict:
    if not auth_enabled():
        raise HTTPException(status_code=400, detail="AUTH_ENABLED is false — no login needed")
    if not auth_manager.verify_password(body.username, body.password):
        raise HTTPException(status_code=401, detail="invalid username or password")
    if auth_manager.totp_enabled(body.username):
        if not body.totp_code or not auth_manager.verify_totp(body.username, body.totp_code):
            raise HTTPException(status_code=401, detail="invalid or missing 2FA code")
    token = auth_manager.create_session(body.username)
    response.set_cookie(SESSION_COOKIE_NAME, token, httponly=True, samesite="lax", max_age=SESSION_TTL_SECONDS)
    return {"username": body.username, "is_admin": auth_manager.is_admin(body.username)}


@router.post("/logout")
async def logout(request: Request, response: Response) -> dict:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        auth_manager.delete_session(token)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return {"ok": True}


class TotpVerifyRequest(BaseModel):
    code: str


@router.post("/totp/enroll")
async def totp_enroll(user: str = Depends(require_user)) -> dict:
    uri = auth_manager.start_totp_enrollment(user)
    return {"provisioning_uri": uri}


@router.post("/totp/confirm")
async def totp_confirm(body: TotpVerifyRequest, user: str = Depends(require_user)) -> dict:
    ok = auth_manager.confirm_totp_enrollment(user, body.code)
    if not ok:
        raise HTTPException(status_code=400, detail="invalid code")
    return {"ok": True}


@router.post("/totp/disable")
async def totp_disable(user: str = Depends(require_user)) -> dict:
    auth_manager.disable_totp(user)
    return {"ok": True}


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


@router.post("/password")
async def change_password(body: ChangePasswordRequest, user: str = Depends(require_user)) -> dict:
    """Self-service password change (Settings > Account, David's ask
    2026-08-31, matching Odysseus's Account panel)."""
    if not auth_manager.verify_password(user, body.current_password):
        raise HTTPException(status_code=401, detail="current password is incorrect")
    auth_manager.change_password(user, body.new_password)
    return {"ok": True}


# -- admin: user management (Settings > Admin > Users) ----------------------

@router.get("/users")
async def list_users(user: str = Depends(require_admin)) -> list[dict]:
    return auth_manager.list_users()


class CreateUserRequest(BaseModel):
    username: str
    password: str
    is_admin: bool = False


@router.post("/users")
async def create_user(body: CreateUserRequest, user: str = Depends(require_admin)) -> dict:
    try:
        auth_manager.create_user(body.username, body.password, is_admin=body.is_admin)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


class SetAdminRequest(BaseModel):
    is_admin: bool


@router.post("/users/{username}/admin")
async def set_user_admin(username: str, body: SetAdminRequest, user: str = Depends(require_admin)) -> dict:
    try:
        auth_manager.set_admin(username, body.is_admin)
    except KeyError:
        raise HTTPException(status_code=404, detail="user not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


@router.delete("/users/{username}")
async def delete_user(username: str, user: str = Depends(require_admin)) -> dict:
    if username == user:
        raise HTTPException(status_code=400, detail="can't delete your own account while logged in as it")
    try:
        auth_manager.delete_user(username)
    except KeyError:
        raise HTTPException(status_code=404, detail="user not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}
