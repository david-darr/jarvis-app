# Frontend Style

Last updated: 2026-10-06

## Direction

Kairos uses ink and gold on parchment: warm light grounds, clear hierarchy, soft shadows, and a single gilded hairline on featured surfaces. The ring's point is the only gilded part of the mark. The default is light; a person may still choose a dark custom base in Appearance. No tactical-HUD brackets, neon outlines, glowing dark-first surfaces, or whole-app recoloring in Developer Mode.

The brand package's `BRAND.md` defines the mark, palette and type. Its ceiling-painting reference supplies the cloud-ring sky; the UI uses the sky on Home, onboarding and the splash. Jost carries body and controls; Cormorant Garamond Italic is reserved for display headlines and the tagline. Both fonts are self-hosted.

The chat-focused pass draws on [Zeron](https://github.com/zeronsh/zeron): minimal navigation, restrained header controls, soft user bubbles, and an uncluttered conversation canvas. [Libraries.dev](https://libraries.dev/) informs the border beam; [Obsidian UI](https://www.obsidianui.dev/) informs quiet hover/selection feedback. These are visual references, not copied application code or added React dependencies. Local development changes remain pending David's visual review.

## One shared system

Scope: `static/css/style.css` and `static/js/`. Reuse shared classes and tokens rather than introducing inline colors, shadows, or a one-off look. Add a named reusable component when the system genuinely lacks a shape.

The renderer is native JavaScript, not React. Views build DOM with `el()` and access the backend through `api()` from `static/js/api.js`. Preserve API contracts, authentication, streaming, and actions. Never substitute sample counts or simulated activity for live data in application views.

### Tokens

- `static/css/kairos-theme.css` holds the `--k-*` brand palette and local font faces; `style.css` maps working tokens onto it.
- `--bg` is parchment `#F3EADB`; `--bg-panel-solid` is surface `#FBF6EE`; `--surface-2` is warm hover `#EDE2D0`.
- `--text` is bistre `#3A2A20`, with dim and faint text tokens; the app's faint, accent and success shades are deepened where needed to keep AA contrast on every ground.
- `--accent: #815E1E` is readable gold for links, focus and active states. `--point` is raw gold `#B8893B`, reserved for the mark and non-text details. `--hairline` is the gilded 1px frame.
- `--border`, `--border-strong`, `--ink-rgb` and `--shade-rgb` derive warm lines, washes and shadows; `--danger`, `--success`, `--warn` have readable status colors.
- `--radius: 12px`, `--sidebar-width: 204px`, `--chat-column: 780px`.
- `--shadow-elevated`, `--shadow-lifted`: reserved for floating menus/modals.

The default is Kairos parchment. Color, Image and Flow derive the same working tokens for a chosen base; there is no separate approved dark brand theme. New themes override tokens, not components. No remote fonts.

### Primitives and layout

- `.glass`: legacy name for a solid, subtly bordered panel. No backdrop blur or decorative corners. `.bracket` remains a compatibility class for existing/custom views.
- `.btn`, `.btn.primary`, `.btn.quiet`, `.btn.danger`: hairline-outline secondary, ink-filled primary, text action, destructive action. Use real buttons with accessible names.
- `.card`, `.title`, `.meta`, `.card-row`: shared surfaces and rows.
- `.view-constrained`, `.view-header`: consistent content width and heading. Chat and Calendar have specialized layouts.
- `.disclosure-panel`: native details/summary for secondary creation and connection forms. Main content comes first.
- `.document-grid`/`.document-card`, `.automation-grid`/`.automation-card`: responsive collections.
- Inputs, textareas, selects and `.custom-select`: consistent field and focus treatment. Keep visible labels or accessible names.
- Icons: local stroke SVGs from `icons.js`, using currentColor; no icon fonts/CDNs.

Sidebar groups separate Workspace and Intelligence. On phones it becomes a drawer. Chat has its own history drawer. Settings remains a desktop floating window and mobile full page.

Settings navigation is grouped into Models, Connections, Workspace, Personal, and Administration. Administration is admin-only and Custom Tabs stays behind Developer Mode; grouping changes presentation only and never widens a gate. Its search matches per-section keywords as well as labels, so the words someone actually types find the right panel, and a group heading hides when nothing under it matches. The desktop window is draggable by its titlebar and stays wholly inside the app viewport, re-clamping after a drag, an edge resize, an app resize, and reopening; resize, minimize, and close are unchanged, and the mobile full page has no draggable window.

Settings pages (redesigned 2026-10-05 after Hermes's desktop settings, Codex and Claude) are built only from `static/js/settingsKit.js`, styled in `static/css/settings.css`: a page header (title, one-line description from the section registry, actions), then titled groups of rows with hairline dividers. A row is label and description on the left, control on the right, stacking under the label in a narrow pane; every on/off setting is a switch, states are pills, destructive actions are quiet red buttons. Pages render into the body `selectSection()` hands them and use `page.actions()` for header buttons or `page.sub()` for a sub-page with a way back. Every nav item has an icon. Do not hand-roll cards in Settings. Closing the desktop window (✕, Escape, or a click outside) only hides it, so you stay on the tab you were on. On phones Settings opens on a list of sections (search, grouped rows with icons and ›); a row opens its page full screen under a top bar whose back arrow goes up one level (sub-page to page, page to the list, the list to the tab Settings was opened from). Header actions and form buttons run full width there, and a lone switch or state stays on the right of its row.

### Collapsible sidebar

The header's panel toggle collapses the desktop sidebar from `--sidebar-width: 204px` to `--sidebar-rail-width: 52px` over 260ms using `--ease-out`. The content naturally takes the released space; do not overlay the rail or remount the current view. Labels fade while icons remain usable. Account, Settings, and Developer Mode remain accessible in the rail.

`sidebar.js` owns the local `jarvis:sidebar-collapsed` preference, toggle state, and hover/focus tooltips. Storage failure must not prevent toggling. All navigation buttons have accessible names, custom tabs without artwork get a fallback icon, and the toggle exposes `aria-expanded`. Tooltips render outside the scrolling sidebar so they cannot be clipped. At 768px and below the desktop preference has no visual effect: keep the full labeled mobile drawer. Reduced motion removes the transition.

## Home

One full-width Home card holds the Overview header, serif tagline, conversation action, next task and quiet cloud-ring sky. A faint gradient keeps the copy legible over the sky. The sky drifts slightly; a small gold point moves only while work runs, warns on attention and dims offline. Pause stops movement, and the control is hidden when system reduced motion is active. A summary strip below the card and a two-column workspace expose conversations, open notes, enabled automations, the next seven days, projects, models, recent activity, and connected systems. On phones these stack in normal flow.

Summary requests are read-only; missing data gets an unavailable state, not a fabricated zero. Home links use `jarvis:navigate` with a real session/project/Library section when applicable. Date-only calendar entries are local days. Clear countdown and refresh timers when leaving the view.

## Chat and motion

A new chat contains only the centered composer and a quiet header. The default Chat canvas uses the same cloud-ring sky as Home under a warm parchment wash; Color, Image and Flow keep their chosen backgrounds. No welcome artwork, headline, or starter chips. The first message moves the same live composer beneath a centered reading column with soft user bubbles and unboxed assistant responses. Its attachment strip, model controls, and keyboard hint travel together; no cloned input or draft-resetting remount. Layout changes use a 380ms position animation, skipped under reduced motion. A ResizeObserver keeps the Latest button above a growing composer and is disconnected on unmount.

Chat history is independently collapsible with the header's history button. Desktop starts tucked away unless the user saved an expanded preference under `jarvis:chat-history-collapsed`; this does not change the global sidebar preference. Its 236px panel transitions to zero width over 260ms. Hidden history is inert and excluded from keyboard navigation. At 768px and below, the same button opens the history drawer with a close action, Escape dismissal, bounded keyboard focus, and focus restoration. New chat clears the current draft and returns to the centered landing without creating a stored session. Opening an existing empty session also centers the composer. Choosing a model or sending the first message can create a session; both preserve staged attachments and the draft until send.

### Chat upgrade — model selection, rich rendering, and file previews

- `chatContent.js` renders assistant Markdown through locally bundled Marked and DOMPurify. User messages stay literal. Code has highlighting and a copy-original-source action; tables scroll within the reading column. Raw HTML styles, scripts, application-local navigation, and remote image loads are not allowed in responses.
- CLI endpoints have a second composer control listing selectable models with a search field, each model's own reasoning levels, and a collapsed custom model-ID field. A session's `model_override` is `null` for the endpoint setting, `""` for the CLI default, or a specific ID. API/local endpoint configuration is unchanged. There is no hardcoded claim about account model availability.
- The model list comes from `/api/models/catalog`. Codex entries are read from the Codex CLI's own local catalog, so their names, reasoning levels, and context sizes are provider data. Claude has no equivalent local catalog, so its entries are curated and marked estimated. Effort levels are per model, not per provider: a level one model advertises may be invalid on another from the same provider. Selecting a model clears the chosen level. An unset level sends nothing and leaves provider behavior unchanged. The server validates every level against the catalog because the Codex CLI accepts unknown config values without complaint. An empty catalog leaves the custom model-ID field as the only control rather than blocking selection.
- The composer shows a context meter for the current chat: the last completed turn's prompt occupancy against the model's effective capacity. This is not cumulative token spend, and historical turns are never summed into it. The reading is stored per session, so it survives reload, stays correct when switching chats, and falls on its own after a compaction. A percentage appears only when both the occupancy and a capacity are known; a known occupancy with no published capacity shows the token count alone, an estimated capacity is labeled as such in the tooltip, and a chat with no reported usage shows nothing at all.
- Claude streams visible text deltas, with completed-block deduplication. Codex streams completed agent messages. The renderer coalesces updates and follows the bottom only while the reader is already near it; otherwise show Latest. A disconnected stream is not completion. Interrupted partial replies persist with a status label.
- Generated links become file cards. The side panel previews images, Markdown, text/code, static HTML, and PDF pages. HTML has an opaque sandbox with no scripts, links, forms, or remote assets. PDF.js renders one page at a time with page controls and an accessible text disclosure. Unsupported files have download-only cards. Text previews over 2 MB use the same download fallback.
- Office files preview as structure, extracted locally and never uploaded to any converter: XLSX as sheet tabs over a scrollable grid, DOCX as a reading view preserving the real order of paragraphs and tables, PPTX as per-slide navigation with speaker notes, CSV as a grid with a source toggle. The panel states plainly that PPTX shows text and structure only, because no slide renderer is bundled. Partial views name the real totals rather than implying the file ends where the preview does. Formulas are never evaluated, cached values are read instead, and macro-enabled formats are not previewed at all. Archives are rejected before parsing if their entry count, expanded size, or compression ratio looks unsafe. Raw Office bytes are only ever served as a download.
- A side browser shares the right-hand region with the file preview, one pane at a time, opened only by an explicit action: the composer's browse item, or activating a link in a reply. In the desktop app it is a native view on its own session partition with no preload, no node integration, and its own cookie jar; only http and https load, loopback and the backend's own origin are refused, permissions are denied without prompting, downloads are handed to the real browser, and the view is destroyed on close, session switch, tab change, and quit. Because it is a native layer above the page, Settings and modals hide it rather than stacking over it. The web client falls back to a sandboxed iframe, always offers open-in-new-tab because framing refusal cannot be detected from JavaScript, and says so when a page does not arrive.
- Preview/download routes require authentication and check that the file belongs to the selected session, including older Markdown-linked files. The new publish endpoint accepts files only within that session's workspace, excludes hidden paths, and caps files at 25 MB. Claude's existing save-file tool and Codex's `save_generated_file --path` command register real files, never invented URLs.
- Session model changes, transcript mutations, and connection-setting changes are rejected while a turn is active. Changing endpoints starts a fresh provider connection and replays saved conversation text as needed; selecting an explicit model within Codex retains its thread. Returning to CLI defaults may require transcript replay into a fresh thread.
- Each chat has a Mode control in its main or side composer. Base keeps the current permission behavior and is the default for new chats and forks. An admin may select Auto to approve that chat's permission requests, including requests made after the model reads outside content; the choice is shown beside the composer and is saved with the chat. Changing modes waits for an active turn to finish. Codex Auto launches with native approval prompts and its workspace sandbox disabled; returning to Base starts a fresh Codex thread with its original workspace sandbox. The mode does not change other chats, global grants, disabled tools, or admin-only tool access. A chat opened by a non-admin runs in Base even if an admin previously selected Auto.
- A failed Chat turn shows its error and offers configured alternatives. The person chooses a model explicitly; Kairos does not switch providers during a running turn. An empty failed reply can be removed and retried through a fresh provider connection with its saved attachments and references. Partial text or recorded tool activity stays in the transcript, and switching models applies to the next message. Name a rate limit when the provider exposes it.
- A right-edge timeline rail appears when a chat has at least eight saved user messages and enough content to scroll, on viewports wider than 1100px. It shows each user turn up to 16, then samples roughly 16 turns while keeping the first and last; marker positions follow the message positions in the full transcript. A marker jumps to its message, and the active marker follows reading position. The header's Summary button opens a popup that generates a concise summary of saved chat text on demand with the selected model. Cache that text per session, label it stale after new messages, and let the person update it. Summarizing never changes the live conversation context. Images and files without saved text are identified as unavailable to the summary. Claude and API summaries run without tools; Codex summaries use a detached ephemeral read-only CLI run in an empty working directory because its CLI cannot disable native tools.

Styles live in `static/css/chat.css` and reuse the shared tokens. Preview panels close and release PDF workers when switching sessions or leaving Chat. Escape closes the panel and restores focus. On small screens the preview fills the Chat area.

Rebuild browser dependencies with `npm ci` then `npm run build:chat`. Generated local bundles and third-party license notices under `static/js/vendor/` ship with the static app; end users need neither Node nor a CDN. PDF code loads only when a PDF is opened.

Use `scripts/ui-smoke.cjs --update-chat-image` to refresh only the shared website/GitHub Chat screenshot from the full app with synthetic data. The default test run does not change published image assets.

Run `python scripts/test_chat.py` using the project environment for isolated backend tests, and `electron/node_modules/electron/dist/electron.exe scripts/chat-smoke.cjs` for the dedicated browser checks. Results and screenshots go under ignored `data/chat-review/`. Both suites use synthetic data; they do not verify account-specific model availability or authorize live model calls.

Implementation references: [Codex CLI flags](https://learn.chatgpt.com/docs/developer-commands?surface=cli), [Claude streaming events](https://code.claude.com/docs/en/agent-sdk/streaming-output), [Marked sanitization guidance](https://marked.js.org/), and [PDF.js rendering](https://mozilla.github.io/pdf.js/examples/).

The `.border-beam` composer implements the requested Libraries.dev-style border effect natively, without adding a React wrapper to this non-React app. A masked conic gradient animates a registered CSS angle around the border at 0.4 opacity, rising to 0.7 on focus. It must not intercept input or clip the model menu. It pauses while the document is hidden; reduced-motion makes it static. This is not the border-beam npm package and does not expose its React props.

`kairosSky.js` mounts the Home sky and small status point with no Three.js dependency. It follows work and system status, supports pause and reduced motion, and removes its scene on unmount.

The vault uses colored note triangles, folder circles, faint edges, and selective labels. Search and Browse vault provide keyboard-accessible alternatives to canvas interaction. It fits while settling, yields camera control when explored, and stops its simulation when settled. Preserve drag, zoom, browse, read, and edit behavior.

## Development copy

A named instance (`npm run start:dev`, electron/instance.js) labels itself everywhere a person could mistake it for the real app: a DEV badge beside the sidebar brand (`.instance-badge`, accent outline, no fill), the window and tray named "Kairos (dev)", the page title, and the sign-in card heading. The backend reports the name as `instance` on `/api/auth/status`; the real app reports an empty string and shows none of this. Both editions keep their existing `%APPDATA%\JARVIS` and `%APPDATA%\JARVIS-dev` data folders despite the display rename.

## Tasks: run history

Every scheduled task and board card run records its start and end, duration, outcome (Succeeded, Failed, Didn't finish, Stopped), model and card attempt; history is kept per task (50 runs each). `static/js/runHistory.js` renders it: a "Run history" disclosure on each board card, loaded when opened and kept open across the board's polling, and "Run history (n)" under each scheduled task beside its last-run line. Newest first, ten until "Show all", each row opening to its output. Records from before start times existed show "duration unknown" rather than a guess. A run cut off by the app closing is shown as Didn't finish.

Durable work (roadmap phase 4, 2026-10-06):
- **Stop.** A running task shows Stop in place of Run now, and Stop also sits beside "Running..." during a Run now. A running board card shows Stop beside "Working on it". A stopped card goes to Blocked.
- **Polling.** The scheduled list polls every 5 seconds while anything in it runs.
- **Late runs.** A scheduled run that began more than 5 minutes after its time reads "late: due <time>" in its row.
- **Deliveries** come from the outbox, as a `.run-delivery` line in the run's row:
  - "delivered to <channel>";
  - "didn't go through yet; trying again at <time> (attempt n)";
  - "failed for a day and was given up", with **Send again**;
  - "Kairos closed while sending; it may not have arrived", with **Send again**.

  The last-run line repeats a failure in red.

## Agents

Named background workers (services/agent_service.py) in the sidebar under Intelligence, with a count of what waits on the person. The list shows each agent's avatar (its initial on its chosen color), role, status (Idle, Working, Needs you, Off, Done for today) and runs today, with the cross-agent inbox above. An agent's page has two tabs: **Chat** (default; the agent's own chats beside one conversation, `static/js/agentChat.js`, streaming through `chatStream.js`) and **Work** (inbox, standing goals, jobs, editable memory, run history, settings; its label carries the count). Only the header and Work redraw while the agent works, so an open chat is never rebuilt. Agent chats never appear in Chats, Home, Library's files by chat or `@` references; deleting an agent moves its chats into Chats.

Agents always run in Auto on every model kind (David, 2026-10-05): nothing they do waits for approval, every decision is audited, Codex agents run without their workspace sandbox, and chats with an agent start in Auto. In exchange only admins can create and direct agents. The inbox therefore holds only questions, reports and results to review. A goal reports only when its reply is not `[SILENT]`.

**Teams** (agents phase 5, 2026-10-05) are Swarm companies, listed below the agents and opened inside the Agents tab through `views/swarm.js` in its embedded mode (`jarvis:navigate` with `team: <id>`, or `team: "new"`); Swarm has no sidebar tab of its own. In a team's setup each teammate can be one of your agents ("Agent" picker, admin only): it takes the agent's name, role and model, and its prompt carries the agent's identity and notes, but on a team it works only with Swarm's tools, never its Auto tool set. The team page's middle pane is the **Team thread**: teammate messages, findings, proposals, revision notes, work handed in, reviews and endings in time order, with a "To" picker (the lead, everyone, or one teammate). What happens on a team comes back to the agents: a revision note lands in that agent's Corrections, a team that needs you asks through its lead's agent (or its first linked one), and a finished mission or shift is a report in every linked agent's inbox with one notification. An agent's Work tab lists its teams.

## Tasks > Triggers

`static/js/views/taskTriggers.js`, built from the Settings kit (`settingsKit.js`): webhook triggers (services/trigger_service.py) as a list (source badge, what each does, last event, "Asks first" or "Runs straight away", an on/off switch), with anything waiting for approval above it (Approve, Skip). A trigger's page shows its address with a copy button, Behaviour (On, Run straight away with a plain warning), Filters and wording (events, one field condition, card title and instructions templates) and its recent events with an outcome pill each. The secret is shown only right after creating a trigger or making a new one, with the setup steps for GitHub or a generic sender. Board cards made by a trigger say "from trigger <name>", and an agent's Work tab lists the triggers that start its work.

## Settings > Channels

`static/js/views/settingsChannels.js` lists Discord bots and every other connector (core/connectors) in one list, a row each: platform initial, name, platform and allowed-sender count or "send only", a status pill (Listening, Ready, Problem with its reason, Off, Not connected) and, for connectors, an on/off switch. A row opens the channel's own page (status, webhook address, settings built from the fields the connector declares, Send test, Remove; Discord adds its default model and channel overrides). "Add a channel" opens a picker of platform tiles grouped as two-way, two-way through a webhook, and send-only; empty groups are left out. Secret fields never show saved values ("Saved; leave blank to keep"). The list refreshes every five seconds only while it is on screen, so a form being edited is never wiped.

## Settings > Added Models > Helpers

The last group on Added Models (roadmap phase 5, 2026-10-06; core/helpers.py) is one row, "Model for helpers", with a select:
- **"Automatic"**, named after the first local model. It is the default.
- **Each local and API model.**
- **"Off".**

Claude and Codex are never offered, because their built-in tools can't be limited to reading; the backend refuses them too. Helpers have no page of their own. Their work shows in the chat as the `delegate` and `helper_results` tool calls.

## Tool Store: health and review

Roadmap phase 6 (2026-10-06): core/integrations.py and core/mcp_client.py. Every added MCP server's card shows a `.tool-store-health` line with a Check button:
- **Working:** the usable tool count and when it was checked, in green.
- **Not responding:** the error, in red. The card's badge says "Not responding", never "Connected".
- **Not signed in.**

When a server's tools are new or changed since they were pinned, a `.tool-store-held` box lists each one: its name, "new" or "changed since you added it", and its description, with Accept, plus "Accept all" when there are several. Held tools stay unavailable to every model until accepted.

A skill whose SKILL.md can't be read shows as a `.tool-store-broken` card: a "Can't be read" badge, the reason, and Delete. A skill's frontmatter `version` follows its source label.

## Settings > Hooks

`static/js/views/settingsHooks.js` (Administration, admin only) lists lifecycle hooks (services/hook_service.py) a row each: a badge for what it does (W web address, N vault note, C channel, > command), the event, the last run, the last outcome as a pill (OK, Blocked, Failed, Timed out) and an on/off switch, with Pause all and Add a hook in the header and a plain note of what hooks cannot see (Codex's own tools). A hook's page shows When (On, event, tools, where), Does (the exact command, address or note; a signed post says so and never shows its secret) and its recent runs, with Send a test event, Edit and Delete. The add form can start from an example and shows only the fields for the chosen event and kind. Saving a new or changed command always opens a confirmation that shows the exact command and when it will run.

## Lifecycle and verification

Each navigation owns a fresh root; delayed work must not overwrite a newer view. Resource-owning views return cleanup functions for listeners, timers, observers, and graphics. Home returns cleanup synchronously while requests are pending.

Run `electron/node_modules/electron/dist/electron.exe scripts/ui-smoke.cjs` for isolated renderer checks. It serves synthetic fixtures on a temporary loopback port, blocks other network requests, and never contacts the live backend. Screenshots and a JSON result go into a unique directory under ignored `data/ui-review/`. Check desktop/mobile layouts, empty/error states, navigation, vault search/read, and composer interactions. These checks do not replace David's review with real account data or authorize a build, commit, or deployment.

## Website and GitHub imagery

`docs/index.html`, `docs/style.css`, and `docs/site.js` are the static public website, with no build step or external font/runtime dependency. Match the Kairos parchment, bistre, ink-filled pill buttons, wordmark, local fonts, sky and motion preferences. Monospace is limited to short section labels. Preserve the download, source, and installation links; do not imply the development UI is already in the packaged release. The site screenshots come from isolated synthetic `ui-smoke` captures, never personal data.

The five-button preview switches between Home, New chat, Conversation, Vault, and the collapsed sidebar. Use real buttons with a pressed state, keep the image's alternative text and full-size link synchronized, and keep the default screenshot usable without JavaScript. Maintain mobile navigation, keyboard focus, a skip link, image dimensions, and reduced-motion behavior.

## Appearance (Settings > Personal > Appearance)

Four background modes: Kairos, Color, Image, Flow. Kairos is parchment. Flow warms a light custom base toward bistre and gold and keeps dark custom bases usable. The preview shows the tagline. Every setting is a local-device preference, stored per signed-in username in localStorage, with an uploaded image held as a Blob in IndexedDB. The `jarvis:` keys and `jarvis-appearance` database stay for existing preferences. Nothing is uploaded and no server setting exists; the panel says so, because "appearance" reading as an account-level setting would be misleading.

The sidebar is always the chosen colour darkened by 18%, and foreground colours flip for light backgrounds so text stays readable against either. Content panels keep solid backgrounds regardless of mode: a shader or photo behind body text is not worth the legibility. Image mode accepts PNG/JPEG/WebP only, up to 12 MB and 40 megapixels, normalised to 2560px on the longest edge and decoded through `createImageBitmap` into a canvas, never assigned as an image URL, so the CSP stays unwidened.

Flow is a native WebGL fragment shader with no added renderer dependency. Distortion, swirl, grain mixer and grain overlay are independent uniforms, and each must visibly change rendered pixels. The canvas caps at 1280px on its longest edge and around 25fps, pauses when the document is hidden, and holds still under reduced motion. **A lost WebGL context must fall back to a static gradient and repaint**, not leave the last frame or an empty canvas behind the whole UI; a restored context returns to the shader. Dials are operable by both pointer drag and keyboard, and the pointer path uses real pointer capture, so it can only be tested with genuine pointer input rather than synthesised events.

## Chat activity

Chat shows an expandable set of transport steps driven by real stream events: sending, connected, waiting, responding, done, interrupted. **It never fabricates reasoning or tool-use steps** — the current backend stream exposes text, done and error only, so anything richer would be invented. The progress element mounts outside the streamed Markdown so token updates never rebuild it, and the floating progress cards keep stable DOM rather than being recreated per token.

## Voice

Two ways to speak to a chat, both transcribed locally. Audio never leaves the machine: the browser records, decodes and resamples to 16 kHz mono WAV, and `core/speech.py` transcribes it with a whisper model on this computer. The engine ships with the app; a model is downloaded on first use from Settings > Speech, so an optional feature does not add half a gigabyte to every installer.

**Dictation** is the mic button in the composer. Click to record, click to stop; the transcript is appended to whatever is already typed rather than replacing it. The model is checked before the microphone opens, so a missing download is reported before someone has spoken into it. A missing engine and a missing model say different things, because one is a broken build and the other is one download away.

**Open Mic** turns a session into a continuous spoken conversation and back again. Its turns are ordinary messages that stream, persist and render like typed ones, so leaving the mode leaves a real conversation behind. Replies are spoken sentence by sentence as they stream, first sentence alone then in two-sentence breaths, with markdown stripped because text written to be read is unintelligible when spoken with its punctuation. Speaking over a reply cuts off both the voice and the in-flight turn: cancelling only the audio would leave the reply generating invisibly and the next thing said would land against stale context. Listening resumes only when the speech queue has fully drained, or the microphone hears the assistant's own next sentence. Pressing it on an empty chat opens a session first, which is how a chat starts as an Open Mic session.

On exit the spoken stretch is folded into a summary written by a brain detached from the session, so it reads the transcript rather than its own memory of it. A failed summary leaves the raw turns in place: verbose beats lost.

**Devices.** The microphone is selectable in Settings > Speech and the choice is stored per device, since it describes the hardware in front of someone rather than an account preference. A saved device that has been unplugged fails loudly and falls back to the default with the stale choice cleared, rather than silently recording from something else. Device names are withheld by the browser until a page has microphone access, so the panel says why and offers to ask rather than listing blanks.

There is no output-device setting, and the panel states this rather than staying silent about it. `speechSynthesis` exposes no sink control at all, so spoken replies always play through the system default. Output routing would only become possible if speech output moved to a TTS engine producing audio data played through an element, where `setSinkId` applies.

Endpointing is done in the browser from audio levels, so the stream never leaves the page until an utterance is complete. State the UI must always convey: listening, transcribing, thinking, speaking. An open microphone hears the room, so this is a mode turned on deliberately and never a default.

## Test commands

Run the Electron suites through `node scripts/run-electron-test.cjs <suite>`. On Windows a direct invocation returns exit 0 while the GUI process is still running, which reads as a pass for a suite that has not finished. The wrapper uses the project's own Electron, waits for the process, passes through output and exit status, and times out at 240 seconds. `browser-smoke --live` adds real websites; `browser-smoke --verify-failure` proves a non-zero exit still survives. Never use npx to fetch a different Electron.

The website and root README share `docs/img/` screenshots. They are actual app renders with synthetic fixture data, not personal account captures or generated mockups. Captions must say development preview/sample data. Keep the existing logo/favicon; replacing the brand is not part of a screenshot refresh.

Refresh the eleven product images only with the explicit `--update-doc-images` flag on the UI smoke command. This includes `chat-new.png` showing the centered composer with both history tucked away and the global rail collapsed. The runner verifies sidebar animation, keyboard operation, persisted state, mobile override, and website previews/images/layout, alongside the existing UI checks. Review the generated desktop/mobile website and app screenshots before handoff. The flag copies only the named product captures into `docs/img/`; a normal test run leaves tracked images alone. Updating these files locally does not publish the website or push to GitHub.

For hidden Electron tests, enable DevTools focus emulation after loading a page and dispatch keyboard input through DevTools. Native-window input does not reliably reach an offscreen window. Wait for width transitions after restoring preferences, and clear test-only focus/scroll positions before taking publication screenshots.
