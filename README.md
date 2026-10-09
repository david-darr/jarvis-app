# Kairos

An open-source AI workspace that runs on your own machine. Keep chats, notes, documents and automations in local files you own, and connect the models and services you choose. Requests to cloud models and connected services go to those providers.

This is the real product (v2). [jarvis-starter-kit](https://github.com/david-darr/jarvis-starter-kit) is the earlier clone-and-wizard v1, kept as a separate working reference.

**[Website](https://david-darr.github.io/kairos/)** · **[Download the latest release](https://github.com/david-darr/kairos/releases/latest)**

![Kairos Home dashboard with its ring mark and connected workspace](docs/img/home.png)

*Development UI preview with sample data. The latest packaged release may differ.*

## What it does

- **Chat** with Claude Code, Codex, an OpenAI-compatible API, or a local model through Ollama or the built-in llama.cpp engine. Choose a model per conversation. Claude and Codex offer searchable model choices, reasoning levels and an exact-ID field. A context meter shows how full the conversation is when usage is available. Attach files and choose a workspace folder. Replies use formatted Markdown and copyable code.
- **Model setup** guides you through Claude (bundled, just sign in), ChatGPT with Codex (install with your permission, then sign in), an API key, or a local model through Cookbook. There is no default model. Pick one above the chat box. Reopen the guide from Settings > Add Models or `/setup`.
- **Commands and shortcuts**: type `/` in the main chat composer for suggestions, or `/help` for help. `/setup`, `/forge`, `/computer` and `/find` open their respective features. Settings > Shortcuts lists the keyboard controls, including the command palette and Quick Entry.
- **Artifact review**: preview generated images, PDFs, Markdown, text/code, static HTML and Office files in a resizable, tabbed document pane. Select slide shapes, cells, lines or page regions and add comments to a message. Requests about a document from another chat return to that chat. Switch versions with the version picker. Office previews run locally; slide layout is approximate.
- **Forge (Preview)**: an admin coding workspace for projects opened from a folder, cloned, or created in Kairos. Sessions use a separate Git worktree by default, a working folder for a branch. Choose Build or read-only Plan. Review Changes, revert files, and undo a turn through checkpoints. Use the editor, terminal and Git panel to stage, commit, switch branches and pull when no merge is needed. Push sends commits to a remote; merge-back brings the session branch into the main checkout. Both ask first; push never forces. Run an app server after approval and view it in App Preview. The terminal is off over Remote Access by default.
- **Computer use**: chats and agents can operate a contained Docker browser with a live view, Stop, Take over and Hand back. An optional agent desktop adds offline document apps. Teach a task by recording your actions during takeover, then review and save a skill. Computer use, the agent desktop, reactions and keeping agent logins are off by default.
- **Agents**: named workers with their own role, goals, memory, chats and inbox. Admins can hand work to an agent with an `@` mention in chat and receive its result there. Agents run in Auto, approving tool requests automatically within their tool and workspace limits, and can work in teams.
- **A side browser** in the chat, opened when you ask for it or when you follow a link in a reply. In the desktop app it runs as a real sandboxed browser view with its own cookies, separate from the app's, and it cannot reach Kairos's own backend. In a plain browser tab it falls back to an embedded frame, with open-in-new-tab always available for sites that refuse to be embedded.
- **Home dashboard**: recent conversations, projects, upcoming events, connected systems, and a live activity feed under a halftone banner of the Kairos figure (or your own picture).
- **A focused workspace**: a centered new-chat composer that settles below the conversation, independently collapsible chat history, and a 52px icon rail. Layout preferences are remembered; mobile keeps full navigation and chat-history drawers.
- **Notes, Calendar, Email, Library**: priorities and todos, CalDAV/iCal calendar sync, IMAP/SMTP email, documents and chat files. Due-dated notes appear on the calendar. Browse and edit Vault notes, or explore their links on a Map. Admins can connect Google Drive for browsing and previews, and read or edit Google Calendar events from Calendar and chat.
- **Tasks**: scheduled automations, either your own prompts or built-in ones (Daily Brief, tidy-up jobs, skill audits). Output can be delivered to a connected channel rather than just sitting in the tab.
- **Kairos Store** in Tool Store: browse community tabs, skills (reusable instructions), tools (MCP servers) and automations beside bundled items. Admins install through the existing scan and review gates. Community automations start off. Build with Kairos uses bundled builder skills. Sign in to GitHub to share your own items after a public file preview; submissions become pull requests for review.
- **One memory, not two**: checkbox items in your vault's `Active Priorities.md` are synced into Notes on every launch, grouped by their vault headings, so asking about your priorities returns what's actually written in your vault. Ticking one in the app ticks it in the vault file too.
- **Channels**: reach the same assistant from Discord, with conversation state shared through the same sessions and vault.
- **Cookbook**: download and run local models without a separate install.
- **Remote access**: reach Kairos from your phone or another computer over [Tailscale](https://tailscale.com), set up from Settings → Remote Access. Nothing is exposed to the public internet: the listener binds only to your Tailscale address, serves real HTTPS, and requires a login.

Memory is a folder of markdown notes, not a database, so it stays readable, portable, and editable by you or any other tool.

| | |
|---|---|
| ![Chat](docs/img/chat.png) | ![Tasks](docs/img/tasks.png) |
| **Chat**: per-conversation model choice, attachments, folder-scoped workspaces | **Tasks**: built-in and custom automations, delivered where you want them |
| ![Vault graph](docs/img/vault.png) | ![Settings](docs/img/settings.png) |
| **Library > Vault > Map**: your notes rendered as a linked graph | **Models**: Claude, local servers, or any API provider |

<details>
<summary>See the compact sidebar</summary>

![Home with the sidebar collapsed to an icon-only rail](docs/img/sidebar-collapsed.png)

![New chat with a centered composer and slim icon rail](docs/img/chat-new.png)

*New-chat development preview with sample data. Chat history and app navigation collapse independently.*

The same workspace, with more room for your content. Demo data shown.

</details>

## Tabs

School and CRM ship as prebuilt tabs. Add or remove them in **Tool Store > Tabs**; an admin can switch them on immediately. Their cards describe what they do and which sources they read.

Use **Build a tab** in the same category to describe a name, icon, behavior and data sources, then choose a model. Kairos starts a chat with a request to follow the bundled `build-custom-tab` skill and build the folder format below. Community tabs are also listed here. **Share to store** previews your tab's files and submits them for review through GitHub sign-in.

Admins can install a `.kairostab` archive or a public GitHub folder link. Kairos checks paths, size, Python imports and the shared skill scanner before installing. Dangerous scans are refused; caution findings need explicit confirmation. Folder user tabs can also be exported from Yours.

User source lives in one `data/tabs/<slug>/` folder, reached by the model's file tools as `custom-tabs/<slug>/`. It contains `tab.json` and `routes.py`, with optional `service.py`, `hooks.py`, `view.js`, `view.css` and helpers. New builds use API version 1 and keep their endpoints under `/api/tab-<slug>/...`. Tab data lives separately in `data/tab-data/<slug>/`, so replacing or removing source keeps the saved data and app updates keep user tabs. Existing tabs in the old split layout continue working. Sidebar order belongs to **Settings > Layout**.

Installed or newly built code awaits an admin's source review in Yours. Approval is tied to the displayed fingerprint and file list; changing any file revokes it. New folder tabs can load after approval without restarting. Changed source that is already loaded needs a restart. Legacy tabs can also require a restart and share a source-tree approval. Approved code runs with Kairos's privileges, so approval is a trust decision.

Tabs use `core.tab_api` as their stable interface to Kairos: confined JSON storage, tab-owned encryption, tool-free completions, read-only mail and selected connection metadata, Library documents, backlog cards, chats and sync. Ciphertexts carry an authenticated `kairos-tab:<slug>:` prefix; a tab cannot decrypt another tab's values or app credential tokens. Core migrates old tab-owned tokens during data adoption. This interface grants no access to Kairos's stored credentials and is not a Python sandbox.

## Install

| Platform | Download |
|---|---|
| Windows 10/11 | `Kairos-Setup-<version>.exe` on the [latest release](https://github.com/david-darr/kairos/releases/latest) |
| macOS (Apple Silicon) | `Kairos-<version>-arm64.dmg` on the [latest release](https://github.com/david-darr/kairos/releases/latest) |

Download it, run it, open Kairos. **No separate Python install is needed.** The app includes its runtime and dependencies. Computer use is optional and requires Docker.

Neither build is code-signed yet. Windows SmartScreen will warn on first run (**More info → Run anyway**); macOS Gatekeeper will block it (right-click the app → **Open**, or allow it under System Settings → Privacy & Security). Intel Macs aren't supported yet: the macOS build is Apple Silicon only.

First launch offers a model setup guide, then vault and optional connection setup. Claude Code is bundled. The guide can install Codex after you approve the command or download. You can also test an API key or download a local model through Cookbook. Setup can be skipped and reopened later. New chats have no default model; choose one above the chat box.

Your data stays in `%APPDATA%\JARVIS\data` (Windows), the existing JARVIS location. Kairos keeps that path so an upgrade retains your chats, notes, credentials, and encryption key.

On Windows, new versions download in the background and install when you quit. On macOS, download the new version and replace the app.

## Developing

Requirements: **Python 3.12+** and **Node 22.12+** (Node 24 recommended). The development runtime is pinned to Electron 43.7.0; `npm install` in `electron/` downloads that runtime through the project's postinstall step.

**Backend:**
```
pip install --require-hashes -r requirements.lock
uvicorn app:app --host 127.0.0.1 --port 8420
```
`requirements.lock` pins every package, dependencies included, to an exact version and file hash, and is what the installer's bundled runtime is built from. To change a dependency, edit `requirements.txt` (every line needs an upper bound), regenerate the lock with the command in its header, and commit both.
Then open http://127.0.0.1:8420, or start the desktop shell, which spawns the backend for you:

**Desktop shell:**
```
cd electron
npm install
npm start
```

In dev the shell uses your `.venv`; a packaged build uses its own bundled runtime.

`npm start` runs as the real app: it uses the same data folder (`%APPDATA%\JARVIS` on Windows), instance lock and port 8420 as an installed Kairos, so the two cannot run together and anything you try touches your real data.

**Development copy:** `npm run start:dev` starts "Kairos (dev)" beside the real app, with its own data folder (`%APPDATA%\JARVIS-dev`), port 8430, no auto-update and no global Quick Entry shortcut. The sidebar shows a DEV badge. Fill its data folder first:
```
python scripts/dev_instance.py fresh     # empty
python scripts/dev_instance.py copy      # a copy of your real data
python scripts/dev_instance.py status
```
`copy` switches off everything in the copy that would act by itself or reach back into the real app: scheduled tasks and Ready cards, Discord bots, remote access, Swarm schedules, and chats' links to Claude/Codex threads. It also copies the vault and points the copy at it. Model connections and signed-in accounts are copied as they are and are the real ones. An existing copy is moved aside with `--replace`, never deleted.

**Building the installer:**
```
cd electron
npm run dist
```
`predist` runs `scripts/build_runtime.py` first, which downloads an embeddable Python (checked against a pinned SHA-256), installs `requirements.lock` into it with `--require-hashes`, and verifies the result can import the app's dependency graph. That runtime (~400MB) is what gets bundled. Installed size is roughly 620MB.

> **Windows note:** building the NSIS installer requires **Developer Mode** enabled (Settings → System → For developers), or an elevated terminal. electron-builder's signing toolchain contains macOS symlinks, and Windows blocks symlink creation for non-elevated users without it. This only affects *building* the installer: the app itself and `win-unpacked/` build fine either way.

## Access and security

- By default (`AUTH_ENABLED=false`) the app runs as a single trusted local user with no login: the sane default for a desktop app on your own machine.
- Set `AUTH_ENABLED=true` to turn on real accounts: bcrypt password hashes, session cookies, optional TOTP 2FA, and an admin/non-admin split. **Use this for any setup reachable beyond localhost.**
- **Remote access** (Settings → Remote Access) binds only to your Tailscale interface address (never `0.0.0.0`), serves a real Tailscale-issued TLS certificate, and refuses to start unless a login exists. `scripts/run_remote.py` is the equivalent CLI path for development.
- TLS certificates are stored in your data directory, outside the app folder and packaged builds.
- Kairos's stored credentials (email passwords, API keys, bot tokens) are encrypted at rest with a key generated per install. The `data/` folder is git-ignored and excluded from packaged builds. Claude and Codex keep their sign-ins in their own vendor-managed locations.
- The packaged desktop app also uses a private per-launch access cookie when accounts are off. Use the tray's **Open in browser** to grant your browser access. A backend started without that desktop secret trusts local requests.
- Forge's terminal is admin-only and belongs to the person, not the agents. Settings > Forge can allow it over Remote Access. It runs a real shell with your machine's permissions; its filtered environment does not pass down Kairos tokens or API keys.
- Computer use requires Docker and never falls back to your desktop. Its browser cannot reach local or private network addresses. Passwords, payment details and codes require takeover. Inspected buy, send and post controls stop for the person even in Auto. An agent's optional kept browser profile can contain signed-in sessions; **Forget logins** removes it.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `AUTH_ENABLED` | `false` | Turn on real accounts + login |
| `APP_PORT` | `8420` | Fallback address for helper processes (Codex's write tools) before the backend has seen a local request. The server's own port comes from its launch command (`--port`). |
| `JARVIS_DATA_DIR` | in-repo `data/` | Where all runtime state is stored. The desktop app sets this to the per-user app-data location automatically. |
| `JARVIS_BACKEND_URL` | unset | Point the Electron shell at an already-running backend instead of spawning one |

## Layout

```
app.py         FastAPI entry point: wiring only
core/          Brain, auth, sessions, scheduler, channels, event bus
routes/        HTTP API, one module per domain
services/      Domain logic: routes and agent tools are both thin adapters over this
static/        Frontend (no build step: plain ES modules + CSS)
mcp_servers/   Capability servers exposed to the agent
scripts/       Standalone CLI utilities
specs/         Living architecture docs
```

## Status

2.0.0 includes Forge with a Preview label, the community Store, guided model setup, slash suggestions, computer use, agents and artifact review. The desktop build includes its Python runtime. Forge remains Preview while development continues.

macOS builds are produced on GitHub's macOS runners (`.github/workflows/build-macos.yml`), since a DMG can't be built from Windows.

Not yet done: neither build is code-signed, so Windows SmartScreen and macOS Gatekeeper both warn on first run. Intel Macs and Linux aren't built yet: the macOS runtime is fetched for the runner's own architecture, so shipping x86_64 needs a build matrix.

## License

MIT: see [LICENSE](LICENSE).
