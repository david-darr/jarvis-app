"""Security headers + auth dependencies.

get_current_user()/require_admin() are FastAPI dependencies routes use to gate
access. When AUTH_ENABLED=false (the local-desktop default), every request
resolves to the single local user with admin rights — no login screen, no
friction, matching the "single trusted user on their own machine" case.
"""
import re
import secrets
from typing import Optional

from fastapi import Request, HTTPException
from starlette.middleware.base import BaseHTTPMiddleware

from core.auth import auth_manager, auth_enabled, SESSION_COOKIE_NAME, SINGLE_USER, INTERNAL_TOOL_TOKEN
from core.constants import APP_PORT


_NO_CACHE_SUFFIXES = (".js", ".mjs", ".css", ".html")

# The port this process's local listener actually serves on, from the first
# request that arrives over plain http on 127.0.0.1 (the desktop window and a
# dev browser always do). Codex's write tools call back to it (see
# local_api_base). It used to come from APP_PORT alone, a default of 8420 that
# no launcher sets, so on any other port - the dev backend runs on 8421 - the
# calls reached whatever was on 8420 and failed with 401 (reproduced
# 2026-09-22). The remote listener, TLS on a tailnet address, is ignored: the
# callback always goes to the local one.
_loopback_port: Optional[int] = None


def remember_loopback_port(scope: dict) -> None:
    global _loopback_port
    server = scope.get("server")
    if scope.get("type") == "http" and scope.get("scheme") == "http" and server and server[0] in ("127.0.0.1", "::1"):
        _loopback_port = server[1]


def local_api_base() -> str:
    """Where a helper process started by this backend reaches its API. APP_PORT
    stays the fallback for a backend no local request has reached yet (the
    packaged app serves on 8420, which is that default)."""
    return f"http://127.0.0.1:{_loopback_port or APP_PORT}/api"


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        remember_loopback_port(request.scope)
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        # No more inline <script> in index.html as of the app.js SPA rewrite —
        # script-src 'self' alone covers same-origin external modules and
        # dynamic import(), so 'unsafe-inline' is dropped from script-src.
        # style-src keeps it since style.css still uses some inline style
        # attributes from JS-built elements (views/*.js).
        # frame-src (David's ask 2026-09-15, the side browser): the web
        # client has no native browser view, so it falls back to an iframe —
        # and without an explicit frame-src, `default-src 'self'` blocks
        # every remote page outright, making that fallback dead on arrival.
        # Found by scripts/chat-smoke.cjs, which reproduces this exact header.
        #
        # Deliberately the narrowest widening that makes it work: it permits
        # EMBEDDING https pages and nothing else. No script, style, connect,
        # or font source changes, so a framed page still cannot run anything
        # in this origin — it is a separate browsing context, the frame is
        # sandboxed without allow-top-navigation (static/js/browserPane.js),
        # and X-Frame-Options/frame-ancestors below still stop JARVIS itself
        # from being framed by anyone else. http: is excluded so the fallback
        # cannot silently downgrade to a plaintext page.
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; "
            "frame-src 'self' https:"
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
        if path == "/api/chat/artifacts/content":
            # Only the authenticated, session-checked viewer may be framed.
            response.headers["X-Frame-Options"] = "SAMEORIGIN"
            response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'self'; sandbox"
            response.headers["Cache-Control"] = "no-store"
        elif path.startswith(("/generated-files/", "/generated-images/")):
            # Legacy links still work, but generated HTML/SVG must never run
            # with the app's origin (including when opened in a new tab).
            response.headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
            if not path.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
                response.headers["Content-Disposition"] = "attachment"
        if path == "/" or path.endswith(_NO_CACHE_SUFFIXES):
            response.headers["Cache-Control"] = "no-store"
        return response


# What the internal tool token may do: exactly the writes Codex's
# mcp_servers/hive_mind_cli.py makes, none of which needs admin. It used to be
# accepted everywhere as a full admin, and every Codex process holds it, so
# with accounts on any Codex chat - a non-admin's, or one steered by injected
# text - could export the backup or wipe data (reproduced 2026-09-22).
# Anywhere else the header is ignored and the request is judged on its own
# credentials.
_INTERNAL_TOOL_ROUTES = (
    ("POST", re.compile(r"^/api/notes$")),
    ("PATCH", re.compile(r"^/api/notes/[^/]+$")),
    ("DELETE", re.compile(r"^/api/notes/[^/]+$")),
    ("POST", re.compile(r"^/api/tasks$")),
    ("PATCH", re.compile(r"^/api/tasks/[^/]+$")),
    ("DELETE", re.compile(r"^/api/tasks/[^/]+$")),
    ("POST", re.compile(r"^/api/calendar/events$")),
    ("PATCH", re.compile(r"^/api/calendar/events/[^/]+$")),
    ("DELETE", re.compile(r"^/api/calendar/events/[^/]+$")),
    ("POST", re.compile(r"^/api/chat/artifacts$")),
)


def _internal_tool_may(request: Request) -> bool:
    path = request.url.path
    return any(request.method == method and pattern.match(path) for method, pattern in _INTERNAL_TOOL_ROUTES)


def get_current_user(request: Request) -> Optional[str]:
    """Returns the authenticated username, or None if unauthenticated.
    Does not raise — routes that require auth should use require_user()/require_admin()."""
    internal_token = request.headers.get("X-JARVIS-Internal-Token")
    if internal_token and secrets.compare_digest(internal_token, INTERNAL_TOOL_TOKEN) and _internal_tool_may(request):
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
