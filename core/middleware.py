"""Security headers + auth dependencies.

get_current_user()/require_admin() are FastAPI dependencies routes use to gate
access. When AUTH_ENABLED=false (the local-desktop default), every request
resolves to the single local user with admin rights — no login screen, no
friction, matching the "single trusted user on their own machine" case.
"""
from typing import Optional

from fastapi import Request, HTTPException
from starlette.middleware.base import BaseHTTPMiddleware

from core.auth import auth_manager, auth_enabled, SESSION_COOKIE_NAME, SINGLE_USER, INTERNAL_TOOL_TOKEN


_NO_CACHE_SUFFIXES = (".js", ".mjs", ".css", ".html")


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        # No more inline <script> in index.html as of the app.js SPA rewrite —
        # script-src 'self' alone covers same-origin external modules and
        # dynamic import(), so 'unsafe-inline' is dropped from script-src.
        # style-src keeps it since style.css still uses some inline style
        # attributes from JS-built elements (views/*.js).
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'"
        )
        # Odysseus applies this same no-cache rule to .js/.css/.html source
        # files specifically (see specs/frontend.md) — without it, Electron's
        # persistent Chromium profile (unlike the old ad hoc kiosk browser
        # launches, which used a fresh temp profile every time) can keep
        # serving a stale cached script indefinitely after a code change,
        # even across a full app relaunch. Found live 2026-08-31 — a real fix
        # looked like it worked (server confirmed via curl) but the running
        # window was still executing the old, broken app.js.
        path = request.url.path
        if path == "/" or path.endswith(_NO_CACHE_SUFFIXES):
            response.headers["Cache-Control"] = "no-store"
        return response


def get_current_user(request: Request) -> Optional[str]:
    """Returns the authenticated username, or None if unauthenticated.
    Does not raise — routes that require auth should use require_user()/require_admin()."""
    internal_token = request.headers.get("X-JARVIS-Internal-Token")
    if internal_token and internal_token == INTERNAL_TOOL_TOKEN:
        return "internal-tool"

    if not auth_enabled():
        return SINGLE_USER

    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return None
    return auth_manager.validate_session(token)


def require_user(request: Request) -> str:
    user = get_current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="authentication required")
    return user


def require_admin(request: Request) -> str:
    user = require_user(request)
    if not auth_manager.is_admin(user):
        raise HTTPException(status_code=403, detail="admin privileges required")
    return user
