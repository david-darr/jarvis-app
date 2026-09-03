---
description: How to build a new custom tab for jarvis-app (Developer Mode) — the file convention that gets it auto-discovered with zero edits to app.py, app.js, or icons.js.
---

# Building a custom tab

jarvis-app auto-discovers custom tabs from file naming alone — no manifest file, no registration step, no editing any shared file (`app.py`, `static/js/app.js`, `static/js/icons.js`). Drop the right files in the right place and it just appears in the sidebar nav on next server restart.

Use this whenever the user asks for a new tab/workflow — e.g. "build me a School tab that shows my Canvas assignments" or "add a PSA outreach tab."

## The convention

A tab is 2-3 files:

1. **`routes/tab_<slug>.py`** (required) — a normal FastAPI router, same shape as every other file in `routes/`. Must define two names at module level:
   - `router` — an `APIRouter`, prefix it under `/api/tab-<slug>` (or whatever makes sense).
   - `TAB_MANIFEST` — `{"id": "<slug>", "label": "Human Label", "icon_svg": "<svg viewBox=\"0 0 24 24\" ...>...</svg>"}`. `icon_svg` is a full inline SVG string (same hand-drawn stroke style as `static/js/icons.js` — `fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"`), rendered directly by the frontend — this is *why* `icons.js` never needs editing.
   - Gate endpoints with `user: str = Depends(require_user)` from `core.middleware` (same as every existing route), unless the tab genuinely needs admin-only actions (`require_admin`).

2. **`static/js/views/<slug>.js`** (required) — the frontend view. Must export:
   ```js
   export async function render(container, tabId) { ... }
   ```
   `container` is the tab's content element — clear it (`container.innerHTML = ""`) and build into it. Return a cleanup function only if you own a persistent resource (an animation loop, a websocket) that needs tearing down on tab switch; otherwise return nothing. Use the shared helpers from `../api.js`: `api(path, options)` (fetch wrapper, throws on non-2xx, parses JSON) and `el(tag, attrs, children)` (DOM builder — `attrs.text` sets textContent, `attrs.onclick` etc. wire listeners). Read `static/js/views/tasks.js` end to end as the concrete worked example of this pattern (fetch on render, rebuild a list, wire buttons that call `api()` then re-fetch).

3. **`services/<slug>_service.py`** (optional, only if the tab needs its own persisted data) — a singleton class following every existing service's exact shape (see `services/task_service.py` or `services/notes_service.py`): reads its own `data/<slug>.json` once at import time via `core.atomic_io.read_json`, mutates an in-memory dict, writes back via `core.atomic_io.write_json_atomic` on every change, module-level singleton instance at the bottom (`<slug>_service = <Slug>Service()`). This is normal — the file you write lives in `services/` (a directory you have real write access to); the *data file it manages* lives in `data/`, which you don't have direct file access to, but that's fine because you're never touching that file directly — the service code does, at runtime, when the app calls it.

That's the whole contract. No `app.py` edit (an existing one-time hook mounts every `routes/tab_*.py` automatically), no `static/js/app.js` edit (the sidebar fetches `/api/system/custom-tabs` and appends whatever it finds), no `icons.js` edit (the icon travels inline in `TAB_MANIFEST`).

## Reference implementation to imitate

Read these three files together before building a new tab — they're the simplest complete example of the exact pattern above (route + service + view):
- `routes/task_routes.py`
- `services/task_service.py`
- `static/js/views/tasks.js`

## After creating the files

The new router is picked up the next time the server restarts (`core/custom_tabs.py` scans `routes/` at startup) — there is currently no hot-reload for newly *added* route modules, only for edits to already-mounted ones under `--reload` (see `scripts/run_remote.py`'s own comment on its reload limitations). Tell the user their new tab needs a server restart to appear.
