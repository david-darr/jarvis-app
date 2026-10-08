---
name: build-custom-tab
description: Build a Kairos tab or workflow as a self-contained user folder using the stable core.tab_api contract.
---

# Build a tab

A tab is one folder containing `tab.json` and `routes.py`, with optional
`service.py`, `hooks.py`, `view.js`, `view.css`, and local helpers. Use this
when someone asks for a new tab or workflow, such as a coursework tracker.
Write source **only inside `data/tabs/<slug>/`**, reached through Kairos's file
tools as **`custom-tabs/<slug>/`**. The installed data directory may be
`%APPDATA%\JARVIS\data`; ask the app if unsure rather than guessing a path.

Never edit or create anything in Kairos's own folders, including `core/`,
`routes/`, `services/`, `tabs/`, `static/`, `app.py`, `app.js`, or `icons.js`.
Updates replace app source; user tab folders survive them. Never import Kairos
except through `core.tab_api`. Use relative imports for the tab's own files:
`from .service import tracker_service`, `from . import helpers`. Standard
library and available third-party libraries are fine; don't import other tabs.

## Manifest and hooks

`tab.json` is a JSON object. Required fields: `slug` (matches its folder;
lowercase letter followed by lowercase letters, digits or underscores), `name`
(nonempty display name), `version` (nonempty tab version string), `description`
(string), `api` (integer API version, currently **1**), and `hooks` (list of
hook names, `[]` if unused). Optional strings: `icon_svg`, `blurb` (card summary),
`detail` (longer card explanation), and `reads` (sources read). Unknown fields
are inert. An unsupported API version shows "Needs a newer Kairos".

Complete example for `custom-tabs/tracker/tab.json`:

```json
{
  "slug": "tracker",
  "name": "Tracker",
  "version": "1.0.0",
  "description": "Track work and deadlines.",
  "api": 1,
  "hooks": ["start", "stop", "on_message", "calendar_items"],
  "icon_svg": "<svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"1.6\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"M5 6h14M5 12h14M5 18h14\"/></svg>",
  "blurb": "Keep deadlines and follow-ups together.",
  "detail": "Review captured work and mark it complete.",
  "reads": "Reads only the message connections you choose"
}
```

Declare only hooks you implement in `hooks.py`. Exact callable signatures
(sync `def` or async `async def` both work):

- `start()`: initialize when the approved tab turns on; return nothing.
- `stop()`: release background resources when removed, changed or shutting down; return nothing.
- `on_message(source, message)`: passive consumer of copied inbound dictionaries; return ignored.
- `calendar_items(user, start, end)`: return a list of calendar item dictionaries for this user and range.

Each hook has a **2-second limit**; slow or failed hooks are skipped. Keep
module imports quick too. Start background work and return; retain tasks and
stop them in `stop()`. Check `api.is_on` before background side effects.
`on_message` must never reply, intercept, redirect or block the normal chat.
`source` has `kind` (`connector` or `discord`) and `connection_id`; `message`
has `conversation`, `sender`, `text`, `sender_name`, `attachments`, `replying_to`,
and provenance (`message_id`, `thread_id`, `sent_at`, `source_url`,
`source_context`, which may be empty). Capture only sources the user chose.

Calendar `user` is the authenticated username; `start` and `end` are ISO range
strings. Filter by user and range (end exclusive). Each item needs `id`,
`title`, `start`, `end`, `all_day`, `completed`, **`source: "tab"`**,
**`source_label`** (e.g. `"From Tracker"`), and **`toggle_url`** under this tab's
own `/api/tab-tracker/...` prefix. Optional `location` and `description` are
strings. Implement the toggle as an authenticated PATCH accepting
`{"completed": true}` or `false`. Do not point it at another tab or app route.

## Stable API v1

Bind a handle in the tab module that uses it:

```python
from core import tab_api
api = tab_api.for_tab(__package__)
```

`tab_api.API_VERSION` is 1; v1 remains supported across app updates.
Module functions (call as `tab_api.<name>`):

- `for_tab(package)`: bind an approved, enabled registered tab package.
- `require_user`, `require_admin`: FastAPI dependencies for signed-in or admin access.
- `is_admin(username)`: check admin status without exposing account data.
- `wrap_untrusted(label, text)`: delimit external text before giving it to a model.
- `list_models()`: list endpoint `id`, `name`, `kind`, and `model`, without secrets.
- `model_context_size(endpoint_id)`: configured local input window, or `None` for other kinds.
- `await complete(endpoint_id, system, prompt, timeout=180)`: tool-free completion; Codex CLI is unsupported.
- `email_accounts()`: connected mail account IDs, emails and names, without credentials.
- `open_mailbox(account_id, folder)`: read-only IMAP context manager; UID search, fetch of `RFC822.SIZE`/`BODY.PEEK[]`, and `response("UIDVALIDITY")` only.
- `message_time(value)`: parse an RFC mail timestamp with timezone, or return `None`.
- `message_connections()`: connection kind, ID and label for supported messaging sources.
- `documents()`: Library document IDs and titles.
- `document(document_id)`: read one Library document.
- `backlog_card(card_id)`: read a work card.
- `create_backlog_card(title, description, agent_id)`: create a backlog card for an agent.
- `delete_backlog_card(card_id)`: delete a work card.

Bound handle members (call as `api.<name>`):

- `slug`: this tab's identity.
- `is_on`: whether current source is approved and enabled.
- `data_dir`: this tab's runtime store in `data/tab-data/<slug>/`.
- `read_json(name, default)`: read a plain filename inside that store.
- `write_json(name, value)`: atomically save JSON inside that store.
- `adopt_data_file(old_name)`: migrate only this tab's legacy `<slug>.json`, including old tab-owned encrypted values.
- `encrypt(text)`: encrypt this tab's own secret with authenticated tab ownership.
- `decrypt(token)`: decrypt only this tab's tokens, refusing app credentials and other tabs' values.
- `chat_session(key, title)`: get/create a persistent tab chat session.
- `append_chat_message(session_id, role, text)`: append context to chat history without a model turn.
- `register_sync(name, fn)`: register an async daily-sync provider that runs only while the tab is on; app removal unregisters it.

Do not call private names or loader lifecycle internals. This interface exposes
no stored Kairos credentials. Approval runs Python with app privileges; this
is a supported import boundary, not a Python sandbox.

## Routes, storage and secrets

`routes.py` must expose a module-level FastAPI `router`. Keep every endpoint
under **`/api/tab-<slug>/...`** and gate it with `Depends(require_user)` or
`Depends(require_admin)` imported from `core.tab_api`:

```python
from fastapi import APIRouter, Depends
from core import tab_api
from core.tab_api import require_user

api = tab_api.for_tab(__package__)
router = APIRouter(prefix="/api/tab-tracker", tags=["tracker"])

@router.get("/items")
async def items(user: str = Depends(require_user)):
    return api.read_json("items.json", {}).get(user, [])
```

Use `api.read_json`/`write_json` for persisted state, with plain filenames,
never absolute paths or traversal. Scope per-user data to the signed-in user.
An optional `service.py` can own a singleton, read its state once, and save on
every change. Runtime data is created by code, not file-tool edits. Store
tab-owned credentials encrypted with `api.encrypt`/`decrypt`; never return
saved plaintext to the view. Show "Saved; leave blank to keep" instead.

## View contract

`view.js` exports **`export async function render(container, tabId, options)`**.
It is served from `/tab-files/<slug>/view.js`; `view.css` is loaded if present.
Clear the supplied container and build into it. Import helpers by their
absolute app URL: `import { api, el } from "/static/js/api.js"`; relative
`../api.js` resolves incorrectly. `api(path, options)` parses JSON and throws
on network/non-2xx failures; `el(tag, attrs, children)` builds DOM (`text`
sets textContent; `onclick` etc. wire listeners). Use relative imports for
local JS helpers. Use existing `.view-constrained`, `.view-header`, `.card`,
`.title`, `.meta`, `.btn`, `.btn.primary`, `.btn.quiet`, `.disclosure-panel`
and theme tokens such as `--text`, `--bg-panel`, `--border`, `--accent`.
Use currentColor stroke SVGs; the manifest icon needs no shared icon edit.

Read `tabs/school/` (routes, service, sync hook and view) and `tabs/crm/`
(passive capture, background cleanup, calendar items and styles) as worked
examples when available. `static/js/views/tasks.js` is also a useful read-only
example of fetching, rebuilding lists and wiring actions.

Catch request failures **inside the view**, including every async event
handler and fire-and-forget promise. `switchTab()` catches only failures while
awaiting `render()`; it cannot catch later work. Failed GETs show no automatic
toast. Replace "Loading..." with a visible error and retry/reopen guidance,
never leave it loading forever. Show an empty state with a useful next action
when a successful request returns no items; never disguise failure as empty.
Before delayed updates check that the container is still connected and the
request belongs to the current view. Return a cleanup function only when
you own resources (timers, listeners, websockets, animation loops), and
release them on tab switch. Use textContent for external text.

## Review and handoff

After writing, the tab appears in **Tool Store > Tabs > Yours** as **Needs
approval**. An admin reviews the file list/source and approves that exact
fingerprint. Approved folder code loads without a restart or edits to Kairos.
Changing any source file requires approval again. Source in the user data
folder survives app updates; removing/replacing source keeps saved tab data.

Before handing it over, run this checklist using the files you wrote:

- Parse `tab.json`; check required fields, matching slug, API 1 and implemented hooks.
- Check `routes.py` exposes `router`, owns its API prefix and authenticates endpoints.
- Check all Kairos imports use only `core.tab_api`; local imports are relative.
- Confirm every written source file is inside `custom-tabs/<slug>/` and there are no links.
- Check initial loads, empty results and failed requests, including async actions, produce visible states.
- Check delayed updates and cleanup, phone-width layout, and tab-owned encrypted storage if used.
- Tell the user where to review and approve it, and what sources/data it reads.
