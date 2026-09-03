# JARVIS

An open-source, self-hosted AI workspace and agent harness. Everything runs on your own machine: your chats, notes, calendar, email, documents, and scheduled automations all live in local files you own, and you plug in whichever models you want.

This is the real product (v2). [jarvis-starter-kit](https://github.com/david-darr/jarvis-starter-kit) is the earlier clone-and-wizard v1, kept as a separate working reference.

## What it does

- **Chat** with any model you connect — Claude via the Agent SDK, any OpenAI-compatible endpoint, or a local model (Ollama, or the built-in llama.cpp engine). Per-conversation model choice, file attachments, folder-scoped workspaces, and slash commands.
- **Mission Control home** — live system health, what's scheduled next, and a real activity feed of what the system has been doing.
- **Notes, Calendar, Email, Library** — one unified place for priorities and todos (due-dated notes render on the calendar), CalDAV/iCal calendar sync, IMAP/SMTP email accounts, and a searchable document library.
- **Tasks** — scheduled automations, either your own prompts or built-in ones (Daily Brief, tidy-up jobs, skill audits). Output can be delivered to a connected channel rather than just sitting in the tab.
- **Brain** — reusable `SKILL.md` procedures, plus a browsable graph of your Obsidian-style vault, which is where the assistant's long-term memory actually lives.
- **Channels** — reach the same assistant from Discord, with conversation state shared through the same sessions and vault.
- **Cookbook** — download and run local models without a separate install.

Memory is a folder of markdown notes, not a database — so it stays readable, portable, and editable by you or any other tool.

## Requirements

- **Python 3.12+** with the dependencies in `requirements.txt`
- **Node 18+** (only to run or build the desktop shell)

> **Note:** the desktop build does not yet bundle a Python runtime — it launches the backend using the project's own `.venv`, or a `python` on your PATH. A packaged app on a machine without Python and these dependencies installed will show a "backend didn't start" screen. Bundling a standalone runtime is a known, tracked gap.

## Running it

**Backend:**
```
pip install -r requirements.txt
uvicorn app:app --host 127.0.0.1 --port 8420
```
Then open http://127.0.0.1:8420 — or start the desktop shell, which spawns the backend for you:

**Desktop shell:**
```
cd electron
npm install
npm start
```

First launch walks you through onboarding: pick a vault folder and connect at least one model.

**Building a distributable:**
```
cd electron
npm run dist
```

## Access and security

- By default (`AUTH_ENABLED=false`) the app runs as a single trusted local user with no login — the sane default for a desktop app on your own machine.
- Set `AUTH_ENABLED=true` to turn on real accounts: bcrypt password hashes, session cookies, optional TOTP 2FA, and an admin/non-admin split. **Use this for any setup reachable beyond localhost.**
- `scripts/run_remote.py` serves the app over your Tailscale network only (never `0.0.0.0`), with a real Tailscale-issued TLS cert and auth forced on.
- Credentials (email passwords, API keys, bot tokens) are encrypted at rest with a key generated per install. Everything sensitive lives in `data/`, which is git-ignored and excluded from packaged builds.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `AUTH_ENABLED` | `false` | Turn on real accounts + login |
| `APP_BIND` | `127.0.0.1` | Bind address |
| `APP_PORT` | `8420` | Port |
| `JARVIS_BACKEND_URL` | — | Point the Electron shell at an already-running backend instead of spawning one |

## Layout

```
app.py         FastAPI entry point — wiring only
core/          Brain, auth, sessions, scheduler, channels, event bus
routes/        HTTP API, one module per domain
services/      Domain logic — routes and agent tools are both thin adapters over this
static/        Frontend (no build step: plain ES modules + CSS)
mcp_servers/   Capability servers exposed to the agent
scripts/       Standalone CLI utilities
specs/         Living architecture docs
```

## Status

Actively developed and used daily. The desktop shell, all tabs, auth, scheduling, and channels work; the main known gap is the bundled-Python packaging note above.
