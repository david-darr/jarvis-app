# Auth & Security

Last updated: Phase 1, 2026-08-31

## Scope

`core/auth.py`, `core/middleware.py`, `routes/auth_routes.py`.

## Trust Model

JARVIS ships in two access modes, chosen by `AUTH_ENABLED`:

- **`AUTH_ENABLED=false` (default).** A single trusted local user runs the Electron desktop app on their own machine. Every request resolves to `SINGLE_USER` ("local") with admin rights, no login screen, no session cookies. This is the expected default for "download the app, run it, it just works."
- **`AUTH_ENABLED=true`.** Real accounts: bcrypt-hashed passwords, session cookies (7-day TTL, `httponly`, `samesite=lax`), optional per-user TOTP 2FA, admin/non-admin privilege split. This is the mode intended for the plain-web access path ("front door #2" — reachable beyond localhost, e.g. over Tailscale like The Bridge already is today) or for anyone who wants login protection even locally.

Matches Odysseus's own default posture, per the explicit "match what Odysseus uses" auth decision in JARVIS Plan (2026-08-31).

## Sessions

`AuthManager` (`core/auth.py`) owns `data/auth.json` (users) and `data/sessions.json` (session tokens), both written atomically via `core/atomic_io.py`. Sessions are looked up by opaque token; `validate_session()` re-checks the backing user still exists on every call, so a deleted account's cookie stops authenticating on its next use rather than continuing to work — same as Odysseus's own `validate_token` behavior.

## Codex's Tool Token

Codex reaches JARVIS's tools by running `mcp_servers/hive_mind_cli.py`, which posts each call to `POST /api/tools/{name}` (`routes/tool_routes.py`). The only credential that route accepts is the turn's own token (`core/tool_access.py`): issued by `core/codex_brain.py` for one Codex turn, bound to that turn's chat (session, whether the chat is an admin's, its agent), passed to the Codex process as `JARVIS_TOOL_TOKEN` and sent back as `X-JARVIS-Tool-Token`, revoked when the turn ends and expired after an hour in any case, never persisted. The route also refuses any client that is not this computer. Who is asking comes from the token, never from the request, so a Codex chat cannot act as another chat or as an admin. Each call runs through the same tool registry, permission checks, lifecycle hooks and audit as every other model's (`core/tool_registry.py` `dispatch`).

This replaced (roadmap phase 2, 2026-10-05) the process-wide `INTERNAL_TOOL_TOKEN`, which every Codex process held whoever was chatting and which resolved to an `"internal-tool"` user on ten write routes, and a separate Google-only chat token. Neither exists any more; an `X-JARVIS-Internal-Token` header is ignored.

With accounts off (`AUTH_ENABLED=false`, the desktop default) every local request is the single admin user, so a local process, an agent's shell included, reaches the admin API without any token. Closing that is a separate change.

`"internal-tool"` and `"api"` are reserved usernames — `create_user()` refuses to register either, so a real account can never collide with this mechanism.

## Security Headers

`SecurityHeadersMiddleware` sets `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, and a baseline CSP on every response. Will need loosening (nonce-based script-src, etc.) once the real frontend ships with inline scripts/styles — matches Odysseus's own CSP evolution, not a finished policy yet.

## Known Gaps (Phase 1, honest)

- No rate limiting on `/api/auth/login` yet.
- No password-reset flow.
- TOTP backup codes not implemented (Odysseus ships 8 single-use backup codes — worth matching later).
- `require_admin()`'s `SINGLE_USER` bypass means AUTH_ENABLED=false has no real privilege separation at all by design — this is intentional for the local desktop case, but any route that should stay admin-only even in single-user mode needs to be identified explicitly before Cookbook/Gallery (deferred) or any genuinely destructive action ships.
- No first-run UI yet — `/api/auth/setup` exists as an API but nothing calls it from a real screen.
