# Auth & Security

Last updated: 2.0.0, 2026-10-09

## Scope

`core/auth.py`, `core/middleware.py`, `routes/auth_routes.py`; Forge, model setup and contained computer access described below.

## Trust Model

Kairos ships in two access modes. `core/auth.py` enables accounts when either the environment or saved settings enables them:

- **Accounts off (default).** A single trusted local user, `SINGLE_USER` ("local"), has admin rights with no login screen. When Electron starts the backend, a per-launch UI secret restricts access to requests with the app's private, HttpOnly access cookie. The tray's Open in browser hands that access to a browser through a single-use code valid for 60 seconds. A backend started without a UI secret trusts requests as the local admin. This is access control for the desktop window, not separate user accounts.
- **Accounts on.** Real accounts: bcrypt-hashed passwords, session cookies (7-day TTL, `httponly`, `samesite=lax`), optional per-user TOTP 2FA, admin/non-admin privilege split. Use this for access beyond localhost. Tailscale Remote Access uses HTTPS on the Tailscale interface and requires an account.

`require_user()` authenticates; `require_admin()` also checks the user's role. With accounts off, an authenticated local user is always an admin.

## Sessions

`AuthManager` (`core/auth.py`) owns `data/auth.json` (users) and `data/sessions.json` (session tokens), both written atomically via `core/atomic_io.py`. Sessions are looked up by opaque token; `validate_session()` re-checks the backing user still exists on every call, so a deleted account's cookie stops authenticating on its next use rather than continuing to work — same as Odysseus's own `validate_token` behavior.

First-run account setup has a real UI in `static/js/auth.js`. Password reset sends an expiring, single-use code through configured email. A successful reset invalidates existing sessions. The logged-out request returns the same wording whether the account/email matches or delivery succeeds.

## Codex's Tool Token

Codex reaches Kairos's tools by running `mcp_servers/hive_mind_cli.py`, which posts each call to `POST /api/tools/{name}` (`routes/tool_routes.py`). The only credential that route accepts is the turn's own token (`core/tool_access.py`): issued by `core/codex_brain.py` for one Codex turn, bound to that turn's chat (session, whether the chat is an admin's, its agent), passed to the Codex process as `JARVIS_TOOL_TOKEN` and sent back as `X-JARVIS-Tool-Token`, revoked when the turn ends and expired after an hour in any case, never persisted. The route also refuses any client that is not this computer. Who is asking comes from the token, never from the request, so a Codex chat cannot impersonate another chat or gain admin rights beyond its bound identity. Each call runs through the same tool registry, permission checks, lifecycle hooks and audit as every other model's (`core/tool_registry.py` `dispatch`).

This replaced (roadmap phase 2, 2026-10-05) the process-wide `INTERNAL_TOOL_TOKEN`, which every Codex process held whoever was chatting and which resolved to an `"internal-tool"` user on ten write routes, and a separate Google-only chat token. Neither exists any more; an `X-JARVIS-Internal-Token` header is ignored.

With accounts off, the Electron-launched backend requires its UI cookie for ordinary admin APIs. The per-turn tool token remains the Codex tool route's credential. A development backend without a UI secret still treats ordinary requests as the local admin.

`"internal-tool"` and `"api"` are reserved usernames — `create_user()` refuses to register either, so a real account can never collide with this mechanism.

## Security Headers

`SecurityHeadersMiddleware` sets `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer`. The app's content security policy limits scripts to self, permits inline styles, permits HTTPS frames and loopback HTTP preview frames, and permits self/data/blob images. HTML, JS and CSS are served without caching. Artifact content has a separate sandbox policy and same-origin framing; generated files and Google previews receive restrictive sandbox policies. The broad loopback frame directive does not replace Forge's native preview port gate.

## Forge Terminal

`routes/forge_routes.py`, `services/forge_terminal_access.py`, `services/forge_terminals.py` and `services/forge_apps.py`:

- Every terminal route requires an admin and the Remote Access gate. Local means plain HTTP with both the actual listener and peer on loopback, including IPv4-mapped loopback. Host and forwarded headers cannot grant local access. Other connections require `forge_terminal_remote`, off by default in Settings > Forge. The output stream rechecks the gate; disabling it also closes remote terminals.
- The shell starts in the session worktree with an allowlisted environment. Kairos tokens and API keys are not inherited. This is a real shell with the person's operating-system privileges, not a sandbox; its commands can read files or credentials available to that account.
- Up to four terminals belong to one session. Output has a 200 KB replay buffer; input and dimensions are bounded. Closing a tab, ending the session or quitting closes the shell. On Windows, a kill-on-close job object also kills descendants; on other platforms cleanup kills the process group.
- Agents receive no terminal tool. Their own shell tools retain their existing restrictions. Plan mode limits the agent and does not limit the person's terminal.

## Forge Git and Editor

`services/forge_session_git.py`, `services/forge_git.py` and `services/forge_sessions.py`:

- Forge routes require an admin. Git uses argument lists, a filtered environment, no interactive credential prompt, timeouts and output caps. Commit, push, pull and merge retain Git hooks; they do not bypass them.
- Push and merge-back force an approval prompt even in Auto. The prompt identifies the source and destination. After approval, Kairos rechecks the workspace and exact target, including branch heads and push URL or base checkout. A changed target is refused. Push never passes a force option. Pull is fast-forward only and requires a clean worktree.
- Merge-back requires committed session changes and a clean main checkout on the recorded base branch. It refuses an existing merge. A failed merge aborts the merge it started and reports conflicting files.
- Editor saves are confined to the session worktree. Relative paths reject traversal, `.git`, symlinks/junctions, Windows device names, alternate streams, trailing dots/spaces and control characters. Only existing UTF-8 text files up to 1 MB can be saved; binary files stay read-only. Hash and modification-time checks detect changes since opening. Paths are revalidated and the opened file compared again before writing, even after choosing Overwrite.
- Build/Plan is enforced by the server: Plan uses Claude's plan permissions, Codex's read-only sandbox and read-only Kairos tools. Repository instructions are size-capped and wrapped as untrusted content. Changes and checkpoint restoration operate on the session workspace and refuse stale reviews/conflicts.

## Forge App Preview

`services/forge_apps.py`, `electron/forgePreview.js` and `static/js/forgeAppPreview.js`:

- Starting a repository's app runs code on the host and requires admin approval of the command and folder. Approval is tied to the command hash; changed commands ask again. Launch uses an argument list without a shell and a filtered environment. Servers stop on session removal or shutdown, with Windows job-object cleanup. Agents can read their own app's logs but cannot start it through an app tool.
- The desktop viewer uses `persist:forge-preview`, separate from the app and side browser. It has sandbox/context isolation, no preload, no Node, denied permissions and blocked downloads.
- Main-process fetches supply the selected session's allowed ports. Navigation is restricted to HTTP on localhost or 127.0.0.1 at those ports, excluding the backend. The list is refreshed and revoking it closes the view. Other loopback requests are blocked, including backend requests from page scripts. Allowed local HTTP/WebSocket traffic supports the app and live reload. External HTTP(S)/WebSocket resources may load; outside navigation is handed to the side browser.
- The web fallback is an iframe and works only on the computer running Kairos. It does not have the native viewer's request filter.

## Model Setup

`routes/model_setup_routes.py`, `services/model_setup.py` and `static/js/modelSetup.js`:

- Setup endpoints are admin-only. Codex installs force an approval showing the exact command or URL. When npm is available, Kairos runs `npm install -g @openai/codex`; otherwise it downloads an official OpenAI GitHub release binary into Kairos's managed bin folder.
- Direct downloads require HTTPS on allowlisted release hosts, bounded sizes, safe archive contents and a matching SHA-256 digest from release metadata before installation. Missing or mismatched digests refuse installation. This digest check applies to direct downloads; npm uses its normal installation path.
- Launches use argument lists with `shell=False` and a filtered environment. Vendor sign-in runs in a separate console/Terminal window. Kairos checks vendor login state without collecting passwords. Status and progress do not return credential contents. API keys are tested through the provider and saved through the existing encrypted model settings; test responses expose success or a bounded error, not the key or model output.
- The guide creates connections but never sets a default model. Local-model setup uses Cookbook rather than installing another CLI.

## Contained Computer Use

`core/computer.py`, `core/computer_driver.py`, `core/computer_image.py`, `routes/computer_routes.py` and `services/skill_recorder.py`:

- Computer use is off by default. An admin enables it and may separately allow non-admin use. Agent desktop and reactions are separate off-by-default switches. Agent computers are admin-only; a chat computer belongs to its owner or an admin.
- Computers run in hardened Docker containers: read-only root, unprivileged user, dropped capabilities, resource limits, filtered public-network egress on ports 80/443 and no published ports. Loopback/private targets are refused. Docker failure never falls back to the person's desktop. The host filesystem is not mounted except an opted-in agent profile.
- Chats start with blank profiles. Keep signed in is off by default per agent; opting in preserves its profile and desktop Documents in Kairos's data folder. Those browser sessions are sensitive access even though no password is returned to the model. Forget logins deletes the profile. Downloads and desktop files stay in the container/profile; there is no host file-transfer feature.
- Site access and delete/remove controls pass through the permission broker; Auto can approve those requests. Inspected controls matching buy/send/post labels are hard stops even in Auto. Likes, follows and reactions stop unless the admin enables them. These checks inspect controls and form submissions, not every possible meaning of a page action. Password, one-time-code and payment fields, and frames that cannot be inspected, require the person to take over. Private web fields are masked in screenshots/live frames.
- The optional desktop has an allowlist of offline document apps and no terminal or messaging client. Model desktop inputs aimed at the browser are refused so web actions retain their checks. Codex's native computer/browser features are switched off on Kairos launches (`core/codex_features.py`); this is separate from the model's other host tools.
- Take over pauses model computer calls. Person input goes directly to the driver, not to the model or permission audit. Recording is explicit during takeover, capped at 200 steps and held in memory. Private-field text is omitted, sensitive URL values are redacted, and the draft skill needs review. Recorded skills use the imported-skill scan: dangerous content is refused and caution requires confirmation. Runtime hard stops still apply.
- Successful computer actions mark page/desktop content as untrusted for later tool decisions. Computers close on Stop, deletion, shutdown or ten idle minutes; at most two run. Live frames stream only while watched. The last closed frame is retained in memory for ten minutes; chat activity can also retain bounded screenshot thumbnails through `core/computer_history.py`.

## Known Gaps

- No rate limiting on `/api/auth/login` yet.
- TOTP backup codes are not implemented.
- Accounts-off mode has one admin identity, not privilege separation among local people or processes with app access.
- Approved custom tabs run in Kairos's Python process with its privileges; source review and scans are trust gates, not a Python sandbox.
