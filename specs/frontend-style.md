# Frontend Style

Last updated: 2.0.0, 2026-10-09

## Direction

Kairos uses ink and gold on parchment: warm light grounds, clear hierarchy, soft shadows, and a single gilded hairline on featured surfaces. The ring's point is the only gilded part of the mark. The default is light; a person may still choose a dark custom base in Appearance. No tactical-HUD brackets, neon outlines, glowing dark-first surfaces, or whole-app recoloring for tab management.

The brand package's `BRAND.md` defines the mark, palette and type. Its ceiling-painting reference supplies the sky and the Kairos figure. The splash shows the painted sky; Home's banner and the default chat background draw the figure and the sky as halftone (`dither.js`). Jost carries body and controls; Cormorant Garamond Italic is reserved for display headlines and the tagline. Both fonts are self-hosted.

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

Settings navigation is grouped into Models, Connections, Workspace, Personal, and Administration. Administration is admin-only. Tabs are managed in Tool Store > Tabs, with admin-only add, remove, install and source approval actions; sidebar order stays in Settings > Layout. Its search matches per-section keywords as well as labels, so the words someone actually types find the right panel, and a group heading hides when nothing under it matches. The desktop window is draggable by its titlebar and stays wholly inside the app viewport, re-clamping after a drag, an edge resize, an app resize, and reopening; resize, minimize, and close are unchanged, and the mobile full page has no draggable window.

Settings pages (redesigned 2026-10-05 after Hermes's desktop settings, Codex and Claude) are built only from `static/js/settingsKit.js`, styled in `static/css/settings.css`: a page header (title, one-line description from the section registry, actions), then titled groups of rows with hairline dividers. A row is label and description on the left, control on the right, stacking under the label in a narrow pane; every on/off setting is a switch, states are pills, destructive actions are quiet red buttons. Pages render into the body `selectSection()` hands them and use `page.actions()` for header buttons or `page.sub()` for a sub-page with a way back. Every nav item has an icon. Do not hand-roll cards in Settings. Closing the desktop window (✕, Escape, or a click outside) only hides it, so you stay on the tab you were on. On phones Settings opens on a list of sections (search, grouped rows with icons and ›); a row opens its page full screen under a top bar whose back arrow goes up one level (sub-page to page, page to the list, the list to the tab Settings was opened from). Header actions and form buttons run full width there, and a lone switch or state stays on the right of its row.

### Collapsible sidebar

The header's panel toggle collapses the desktop sidebar from `--sidebar-width: 204px` to `--sidebar-rail-width: 52px` over 260ms using `--ease-out`. The content naturally takes the released space; do not overlay the rail or remount the current view. Labels fade while icons remain usable. Account, Settings, and Tool Store remain accessible in the rail.

`sidebar.js` owns the local `jarvis:sidebar-collapsed` preference, toggle state, and hover/focus tooltips. Storage failure must not prevent toggling. All navigation buttons have accessible names, custom tabs without artwork get a fallback icon, and the toggle exposes `aria-expanded`. Tooltips render outside the scrolling sidebar so they cannot be clipped. At 768px and below the desktop preference has no visual effect: keep the full labeled mobile drawer. Reduced motion removes the transition.

## Home

One full-width Home card holds the Overview header with the date and system status, the serif tagline, conversation action and next task, over a still halftone banner of the Kairos figure in the appearance's halftone colors. A parchment wash on the left keeps the copy legible. Appearance can replace the banner with a custom picture and focus point. There is no motion or status point in the banner. A summary strip below the card and a two-column workspace expose conversations, open notes, enabled automations, the next seven days, projects, models, recent activity, and connected systems. On phones these stack in normal flow.

Summary requests are read-only; missing data gets an unavailable state, not a fabricated zero. Home links use `jarvis:navigate` with a real session/project/Library section when applicable. Date-only calendar entries are local days. Clear countdown and refresh timers when leaving the view.

## Model setup guide

`static/js/modelSetup.js` supplies the same guide in onboarding, Settings > Add Models and empty-model states. Onboarding offers Skip for now. The popup uses `openPanelDialog` and `.model-setup-panel`; `.model-setup-cards` is a two-column grid that becomes one column below 600px. Each choice names what the person needs, the cost model and the current detected state. Keep the explanation of CLI, API key and local model in the native details disclosure, not in the main action labels.

Claude needs sign-in to its bundled app; Codex offers installation with an approval prompt and bounded progress output before vendor sign-in. API setup tests the key. Local setup opens Cookbook. Waiting, errors and retry actions are visible in `.model-setup-screen` with a polite live region. Success tells the person to pick the connection above the chat box: setup never assigns a default model. Dispose polling, pending approval UI and the local-model view when leaving.

## Forge (Preview)

`app.js` owns the admin-only Kairos | Forge switch and its Preview label. The collapsed rail uses the Kairos circle mark to switch modes. Forge navigation has Home and a Projects list with a + menu for Open folder, Clone repo and New repo. Removing a project drops its listing and keeps its files. Forge Home retains its composer, project/model and Build/Plan choices, Git activity and repository charts. Widget visibility is device-local.

`views/forgeShell.js` and `forgeRail.js` own the project layout, styled in `static/css/style.css`:

- `.forge-project-sidebar` starts at 228px, resizes from 208 to 520px and resets on double-click. Sessions, Explorer, Changes and Git use tab/tabpanel roles and arrow-key navigation. The panel choice is remembered per project.
- Sessions show the real model, branch, status and change totals. Explorer is lazy and keyboard accessible. Single-click opens an italic preview tab; double-click pins it. Changes opens a line-numbered diff against the session baseline and offers confirmed file reverts. Turn review bars offer checkpoint undo. The backend has a hunk-revert route, but the current diff viewer does not expose a hunk-revert control.
- `.forge-surface-tabs` holds sessions, files, diffs, App Preview and terminals. One split can place a file or preview beside a session. On phones the project panel is a sheet and the main area shows one tab at a time.
- `forgeTranscript.js` uses bordered prompt blocks and unboxed prose over the shared chat halftone. Paths, commands and output use monospace; prose uses the shared sans font. Tool activity comes from stream events and saved run timelines. Working becomes a Worked for disclosure; changed turns offer Undo, Keep and Review. The composer keeps path/branch, model and Build/Plan controls with Send, Stop and Queue. Plan is read-only for the agent. Forge file `@` suggestions are not implemented.

### Editor, terminal, Git and App Preview

`forgeEditor.js` loads bundled CodeMirror on demand, with shared theme tokens, line numbers, syntax highlighting, search and undo. `.forge-editor-toolbar` has Save and a Saved/Unsaved state; Ctrl/Cmd+S saves. Dirty files become pinned tabs and ask before closing. A disk conflict opens Overwrite or Reload, with one error presentation. Binary files remain read-only.

`forgeTerminal.js` loads bundled xterm and its local stylesheet. `.forge-terminal-toolbar` offers New terminal, Copy and Paste, plus the phone keyboard note. Resize fits the visible pane; output reconnects from its cursor. Closing the tab closes its shell. Links open in the system browser. Colours follow shared tokens. Remote refusal stays a plain explanation pointing to Settings > Forge; Plan does not restrict the person's terminal.

`forgeGitPanel.js` renders branch/upstream status, staged and unstaged files, commit controls, branch creation/switch, Push, fast-forward Pull and Merge back. Push and merge approvals name their destination. Ending offers merge-and-remove, keep-branch-and-remove or discard, with confirmation for uncommitted or unmerged work.

`forgeAppPreview.js` owns the Run app approval and Preview tab. The toolbar has path navigation, Back/Forward/Reload, Open in browser and Desktop/Tablet/Phone widths. Logs expose Stop and Restart. The web fallback says it works only on the computer running Kairos. Running apps appear in the project panel and Home. Hide native views while modals cover them, and release views, listeners and process controls through their existing lifecycle.

## Slash suggestions and Settings > Shortcuts

`slashCommands.js` supplies suggestions only in the main Chat composer while its text starts with `/` and has no space after the command name. Reuse the reference popup's classes; each option shows the command, description and usage. It has listbox/option roles and an active descendant. Up/Down selects; Enter or Tab completes; a fully typed command runs on Enter. Escape closes. Hide admin-only commands for non-admins, and cap the popup height on phones.

`shortcuts.js` supplies both `/help` and Settings > Shortcuts. The Settings kit groups rows into Anywhere, Composer, Chat and Documents. Show Cmd on macOS and Ctrl elsewhere. Draw immediately with the default Quick Entry key, then replace it with the desktop's actual configured key. Help for a chat-dependent command explains when a chat is needed.

## Computer use pane

`computerPanel.js` shares `.computer-panel` between chats and agents; styles live in `chat.css`. Show the title, wrapping URL, last action and control state above an 8:5 letterboxed live frame. Take over pauses model input and focuses a keyboard-operable stage; Hand back resumes. Stop closes the computer. Map pointer input to the drawn frame and send person inputs in order. A closed computer retains a labelled last frame rather than appearing live.

Chats auto-open the pane on desktop and offer a watch chip on phones. Computer activity thumbnails sit under replies. Settings > Computer use shows the off-by-default enable, non-admin, reaction and desktop switches, Docker status, kept profiles and running computers. During takeover, Record exposes a red Recording state and Stop recording opens an editable skill review. No recording starts by itself. Dispose the frame stream, retries and review dialog on unmount.

## Chat and motion

A new chat contains only the centered composer and a quiet header. In the default appearance the Chat canvas is halftone: the Kairos figure while a chat is empty and the sky once it has messages, dissolving dot by dot between them (`chatBackdrop.js`). Custom pictures from Appearance can replace either scene. Color, Image and Flow keep their chosen backgrounds. No welcome artwork, headline, or starter chips. The first message moves the same live composer beneath a centered reading column with soft user bubbles and unboxed assistant responses. Its attachment strip, model controls, and keyboard hint travel together; no cloned input or draft-resetting remount. Layout changes use a 380ms position animation, skipped under reduced motion. A ResizeObserver keeps the Latest button above a growing composer and is disconnected on unmount.

Chat history is independently collapsible with the header's history button. Desktop starts tucked away unless the user saved an expanded preference under `jarvis:chat-history-collapsed`; this does not change the global sidebar preference. Its 236px panel transitions to zero width over 260ms. Hidden history is inert and excluded from keyboard navigation. At 768px and below, the same button opens the history drawer with a close action, Escape dismissal, bounded keyboard focus, and focus restoration. New chat clears the current draft and returns to the centered landing without creating a stored session. Opening an existing empty session also centers the composer. Choosing a model or sending the first message can create a session; both preserve staged attachments and the draft until send.

### Chat upgrade — model selection, rich rendering, and file previews

- `chatContent.js` renders assistant Markdown through locally bundled Marked and DOMPurify. User messages stay literal. Code has highlighting and a copy-original-source action; tables scroll within the reading column. Raw HTML styles, scripts, application-local navigation, and remote image loads are not allowed in responses.
- CLI endpoints have a second composer control listing selectable models with a search field, each model's own reasoning levels, and a collapsed custom model-ID field. A session's `model_override` is `null` for the endpoint setting, `""` for the CLI default, or a specific ID. API/local endpoint configuration is unchanged. There is no hardcoded claim about account model availability.
- The model list comes from `/api/models/catalog`. Codex entries are read from the Codex CLI's own local catalog, so their names, reasoning levels, and context sizes are provider data. Claude has no equivalent local catalog, so its entries are curated and marked estimated. Effort levels are per model, not per provider: a level one model advertises may be invalid on another from the same provider. Selecting a model clears the chosen level. An unset level sends nothing and leaves provider behavior unchanged. The server validates every level against the catalog because the Codex CLI accepts unknown config values without complaint. An empty catalog leaves the custom model-ID field as the only control rather than blocking selection.
- The composer shows a context meter for the current chat: the last completed turn's prompt occupancy against the model's effective capacity. This is not cumulative token spend, and historical turns are never summed into it. The reading is stored per session, so it survives reload, stays correct when switching chats, and falls on its own after a compaction. A percentage appears only when both the occupancy and a capacity are known; a known occupancy with no published capacity shows the token count alone, an estimated capacity is labeled as such in the tooltip, and a chat with no reported usage shows nothing at all.
- Claude streams visible text deltas, with completed-block deduplication. Codex streams completed agent messages. The renderer coalesces updates and follows the bottom only while the reader is already near it; otherwise show Latest. A disconnected stream is not completion. Interrupted partial replies persist with a status label.
- Generated links become file cards. The side panel previews images, Markdown, text/code, static HTML, and PDF pages. HTML has a same-origin sandbox for parent-attached picking, with no scripts, links, forms, or remote assets. PDF.js renders one page at a time with thumbnails, zoom controls and an accessible text disclosure. Unsupported files have download-only cards. Text previews over 2 MB use the same download fallback.
- Office files preview locally and are never uploaded to any converter: XLSX has sheet tabs and a grid with row/column coordinates, DOCX retains body order, text runs, lists and inline images, PPTX has positioned shapes, pictures, thumbnails and speaker notes, and CSV has a grid with a source toggle. The slide pane states Approximate layout. Partial views name the real totals rather than implying the file ends where the preview does. Formulas are never evaluated, cached values are read instead, and macro-enabled formats are not previewed at all. Archives are rejected before parsing if their entry count, expanded size, or compression ratio looks unsafe. Raw Office bytes are only ever served as a download.
- A side browser shares the right-hand region with the file preview, one pane at a time, opened only by an explicit action: the composer's browse item, or activating a link in a reply. In the desktop app it is a native view on its own session partition with no preload, no node integration, and its own cookie jar; only http and https load, loopback and the backend's own origin are refused, permissions are denied without prompting, downloads are handed to the real browser, and the view is destroyed on close, session switch, tab change, and quit. Because it is a native layer above the page, Settings and modals hide it rather than stacking over it. The web client falls back to a sandboxed iframe, always offers open-in-new-tab because framing refusal cannot be detected from JavaScript, and says so when a page does not arrive.
- Preview/download routes require authentication and check that the file belongs to the selected session, including older Markdown-linked files. The new publish endpoint accepts files only within that session's workspace, excludes hidden paths, and caps files at 25 MB. Claude's existing save-file tool and Codex's `save_generated_file --path` command register real files, never invented URLs.
- Session model changes, transcript mutations, and connection-setting changes are rejected while a turn is active. Changing endpoints starts a fresh provider connection and replays saved conversation text as needed; selecting an explicit model within Codex retains its thread. Returning to CLI defaults may require transcript replay into a fresh thread.
- Each chat has a Mode control in its main or side composer. Base keeps the current permission behavior and is the default for new chats and forks. An admin may select Auto to approve that chat's permission requests, including requests made after the model reads outside content; the choice is shown beside the composer and is saved with the chat. Changing modes waits for an active turn to finish. Codex Auto launches with native approval prompts and its workspace sandbox disabled; returning to Base starts a fresh Codex thread with its original workspace sandbox. The mode does not change other chats, global grants, disabled tools, or admin-only tool access. A chat opened by a non-admin runs in Base even if an admin previously selected Auto.
- A failed Chat turn shows its error and offers configured alternatives. The person chooses a model explicitly; Kairos does not switch providers during a running turn. An empty failed reply can be removed and retried through a fresh provider connection with its saved attachments and references. Partial text or recorded tool activity stays in the transcript, and switching models applies to the next message. Name a rate limit when the provider exposes it.
- A right-edge timeline rail appears when a chat has at least eight saved user messages and enough content to scroll, on viewports wider than 1100px. It shows each user turn up to 16, then samples roughly 16 turns while keeping the first and last; marker positions follow the message positions in the full transcript. A marker jumps to its message, and the active marker follows reading position. The header's Summary button opens a popup that generates a concise summary of saved chat text on demand with the selected model. Cache that text per session, label it stale after new messages, and let the person update it. Summarizing never changes the live conversation context. Images and files without saved text are identified as unavailable to the summary. Claude and API summaries run without tools; Codex summaries use a detached ephemeral read-only CLI run in an empty working directory because its CLI cannot disable native tools.

Styles live in `static/css/chat.css` and reuse the shared tokens. Files, browser and computer panes keep their per-chat lifecycle. Documents retain their renderers and PDF workers across chat switches and leaving Chat; closing a document tab releases its controller and renderer. Escape closes the visible pane and restores focus. On small screens the preview fills the Chat area.

### Artifact pane

Opening an artifact gives it the remaining width beside a resizable chat column (520px by default). The divider supports drag and keyboard resizing and remembers the width on this device. Maximize hides the chat until Restore. Below 1100px the pane overlays the chat; below 680px it fills the chat area. Tabs retain each file's viewer and navigation state. The version picker keeps comments attached to the exact version reviewed.

Documents belong to the Chat tab across conversations, new chats, and Home → Chats. Hiding the pane parks its live DOM outside the view in a hidden, inert host; state-preserving DOM moves keep HTML frames alive. Returning restores the visible desktop pane, width, active tab and Maximize. At overlay widths (1100px and below), navigation leaves it closed until an explicit reopen. The header's Documents control restores the parked tabs. Files, browser and computer still replace the visible pane, while document tabs are kept. Explicit tab closes remove documents; the pane close/Escape hides them.

The tab strip's + uses `api.js`'s app option menu, extended with a labelled search input. This chat's files come first, followed by recent other-chat groups from `/api/chat/files/library`. Ctrl/Cmd-click and middle-click on file cards or file-list rows open background tabs without changing the active document; file groups offer Open all. At most 20 documents are open. Foreign documents show a small `from <chat title>` button in their header; it opens the origin chat, with “another chat” as the missing-title fallback.

`jarvis:artifact-pane` stores tab origin IDs, exact URLs/names, the active index, Maximize and foreign comment drafts/notes/queues. Every storage access is guarded. Restart validates metadata in parallel, drops 400/404 tabs with one removal toast, and renders only the active document until others are selected. Temporary network/server failures keep tabs. A send queued before restart returns as a draft with a note; it never sends automatically at startup.

Select in the toolbar, or S with the pane focused, turns on picking. Use accent outlines without moving the content; committed comments use dashed outlines while their composer chip exists. Click picks a part, modifier-click adds or removes, Shift-click extends text lines, and drag picks cells or an image/PDF region. Alt-click picks a slide shape's group. The comment box stays inside the pane and docks at the bottom on phones. Add to message stages an editable reference chip; sending remains the composer's action. Escape leaves Select before restoring or closing the pane. Comments have their own limit of ten alongside five ordinary references, and follow the same queue, edit and regenerate lifecycle.

On a foreign document, Add comment instead stages an editable, removable draft in that tab's bottom bar, with the origin title, optional Note for the chat and Send. Drafts retain dashed outlines on their exact reviewed URL. Send calls `chatStream.startTurn` for the origin ID, never the current chat, with ordinary `artifact_comment` references and no client excerpts. Empty notes use “Please apply these review comments.” A successful start clears the draft; failures restore it for Retry. Working, Needs approval (Open chat), Failed and Done follow the origin stream. A busy origin queues the send and offers Cancel queued send; multiple tabs serialize through the same turn registry. Opening the origin chat keeps existing foreign drafts in their bar, while new comments become normal composer chips. On origin completion, that chat's own transcript is fetched for the version picker and Newer version control.

Office previews retain slide geometry, pictures, text runs and document lists; slides say Approximate layout. Static HTML uses a same-origin sandbox without scripts, with all picking listeners attached by the parent. Every picked excerpt is read again on the server and fenced as file content; only the comment is the user's own text.

Rebuild browser dependencies with `npm ci` then `npm run build:chat`. Generated local bundles and third-party license notices under `static/js/vendor/` ship with the static app; end users need neither Node nor a CDN. PDF code loads only when a PDF is opened.

Use `scripts/ui-smoke.cjs --update-chat-image` to refresh only the shared website/GitHub Chat screenshot from the full app with synthetic data. The default test run does not change published image assets.

Run `python scripts/test_chat.py` using the project environment for isolated backend tests, and `electron/node_modules/electron/dist/electron.exe scripts/chat-smoke.cjs` for the dedicated browser checks. Results and screenshots go under ignored `data/chat-review/`. Both suites use synthetic data; they do not verify account-specific model availability or authorize live model calls.

Implementation references: [Codex CLI flags](https://learn.chatgpt.com/docs/developer-commands?surface=cli), [Claude streaming events](https://code.claude.com/docs/en/agent-sdk/streaming-output), [Marked sanitization guidance](https://marked.js.org/), and [PDF.js rendering](https://mozilla.github.io/pdf.js/examples/).

The `.border-beam` composer implements the requested Libraries.dev-style border effect natively, without adding a React wrapper to this non-React app. A masked conic gradient animates a registered CSS angle around the border at 0.4 opacity, rising to 0.7 on focus. It must not intercept input or clip the model menu. It pauses while the document is hidden; reduced-motion makes it static. This is not the border-beam npm package and does not expose its React props.

`dither.js` draws a halftone scene from a source picture in the appearance's halftone colors; Home's banner and `chatBackdrop.js` use it, redraw when the picture or colors change, and release their canvases on unmount.

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

Agents run in Auto on every model kind: broker requests are automatically approved within their tool limits, but computer-use hard stops still require the person. Codex agents keep their workspace sandbox. Chats with an agent start in Auto. Only admins can create and direct agents. The inbox holds questions, reports and results to review. A goal reports only when its reply is not `[SILENT]`. An agent mention in an admin chat hands work over as a card; its status, questions and result return to that chat through `agentHandoff.js`.

**Teams** (agents phase 5, 2026-10-05) are Swarm companies, listed below the agents and opened inside the Agents tab through `views/swarm.js` in its embedded mode (`jarvis:navigate` with `team: <id>`, or `team: "new"`); Swarm has no sidebar tab of its own. In a team's setup each teammate can be one of your agents ("Agent" picker, admin only): it takes the agent's name, role and model, and its prompt carries the agent's identity and notes, but on a team it works only with Swarm's tools, never its Auto tool set. The team page's middle pane is the **Team thread**: teammate messages, findings, proposals, revision notes, work handed in, reviews and endings in time order, with a "To" picker (the lead, everyone, or one teammate). What happens on a team comes back to the agents: a revision note lands in that agent's Corrections, a team that needs you asks through its lead's agent (or its first linked one), and a finished mission or shift is a report in every linked agent's inbox with one notification. An agent's Work tab lists its teams.

## Tasks > Triggers

`static/js/views/taskTriggers.js`, built from the Settings kit (`settingsKit.js`): webhook triggers (services/trigger_service.py) as a list (source badge, what each does, last event, "Asks first" or "Runs straight away", an on/off switch), with anything waiting for approval above it (Approve, Skip). A trigger's page shows its address with a copy button, Behaviour (On, Run straight away with a plain warning), Filters and wording (events, one field condition, card title and instructions templates) and its recent events with an outcome pill each. The secret is shown only right after creating a trigger or making a new one, with the setup steps for GitHub or a generic sender. Board cards made by a trigger say "from trigger <name>", and an agent's Work tab lists the triggers that start its work.

## Library > Google Drive and Calendar

`googleWorkspace.js` uses a left rail for My Drive, Shared with me, Starred, Recent and Trash, a search toolbar and Grid/List buttons with pressed states. Thumbnails, multi-select actions, breadcrumbs and a details pane share the Library tokens. Opening a file uses an in-app viewer: Docs is read-only sanitized HTML, Slides/PDF uses the PDF viewer, and images/text have local rendering. Docs and Slides edits open in Google; Sheets and Forms retain their existing editors. Abort stale previews and release PDF workers when leaving. Google access remains admin-only.

`views/calendar.js` and `googleCalendar.js` provide Month/Week views and coloured Google calendars beside local events and feeds. The calendar list controls visibility. Event dialogs show the target calendar, and Google changes write through to Google. A missing Calendar scope offers Reconnect to add Calendar rather than silently hiding events.

## Settings > Channels

`static/js/views/settingsChannels.js` lists Discord bots and every other connector (core/connectors) in one list, a row each: platform initial, name, platform and allowed-sender count or "send only", a status pill (Listening, Ready, Problem with its reason, Off, Not connected) and, for connectors, an on/off switch. A row opens the channel's own page (status, webhook address, settings built from the fields the connector declares, Send test, Remove; Discord adds its default model and channel overrides). "Add a channel" opens a picker of platform tiles grouped as two-way, two-way through a webhook, and send-only; empty groups are left out. Secret fields never show saved values ("Saved; leave blank to keep"). The list refreshes every five seconds only while it is on screen, so a form being edited is never wiped.

## Settings > Added Models > Helpers

The last group on Added Models (roadmap phase 5, 2026-10-06; core/helpers.py) is one row, "Model for helpers", with a select:
- **"Automatic"**, named after the first local model. It is the default.
- **Each local and API model.**
- **"Off".**

Claude and Codex are never offered, because their built-in tools can't be limited to reading; the backend refuses them too. Helpers have no page of their own. Their work shows in the chat as the `delegate` and `helper_results` tool calls.

## Tool Store: health and review

All, Skills, Tools, Tabs and Automations filters cover bundled and Community items. Community cards show the author, kind, version and install/update/revocation state. Reuse the existing scan/review dialog for installs and updates; an installed community automation starts off. Build with Kairos opens a brief tied to the appropriate bundled builder skill. Share to store uses `storePublish.js`: GitHub device sign-in, an exact public file preview, confirmation and a submission status. Never show sharing as an immediate publication.

Tool Store > Tabs lists Prebuilt and Yours. Build a tab and Install a tab are header actions with forms above the cards. Card footers use real `.btn` buttons in one row: Add and Approve are primary, Remove is quiet danger, Export is quiet. Review files uses `.disclosure-panel`, with a wrapping file list and fingerprint. Status uses the shared `.set-pill` tones: On is ok, Off muted, approval/newer-API states warn, Invalid/Failed error. A failure reason is one line with the full text in its title. `ui-smoke` captures `desktop-tool-store-tabs` and `mobile-tool-store-tabs`.

User tabs are folders in `data/tabs/<slug>/` (`custom-tabs/<slug>/` through file tools), containing `tab.json`, `routes.py`, and optional service, hooks, view, CSS and helpers. Python imports Kairos only through `core.tab_api`; endpoints own `/api/tab-<slug>/...`. Runtime JSON and encrypted tab secrets live in `data/tab-data/<slug>/`. New or changed user source needs an admin's fingerprint-bound approval in Yours. New approved folders load without editing app code or restarting and survive app updates; changing already mounted source requires a restart. Removed, off or changed mounted tabs refuse requests rather than serving old code. The old split layout remains supported for existing tabs; new builds use folders. Developer Mode and the New Tab sidebar entry are retired.

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

The website embeds the generated `docs/demo` app on sample data, with an Open full screen link. Keep its source generated rather than hand-editing it. Maintain mobile navigation, keyboard focus, a skip link, image dimensions, descriptive screenshot captions and reduced-motion behavior. Feature copy follows the implemented release; preserve existing image and card patterns.

## Appearance (Settings > Personal > Appearance)

Four background modes: Kairos, Color, Image, Flow. Kairos is parchment. Flow warms a light custom base toward bistre and gold and keeps dark custom bases usable. The preview shows the tagline. Every setting is a local-device preference, stored per signed-in username in localStorage, with an uploaded image held as a Blob in IndexedDB. The `jarvis:` keys and `jarvis-appearance` database stay for existing preferences. Nothing is uploaded and no server setting exists; the panel says so, because "appearance" reading as an account-level setting would be misleading.

`views/appearancePanel.js` also offers Home, New chat and Conversation halftone picture slots in Kairos and Color modes. Each thumbnail has Replace and Reset; clicking sets the crop focus. PNG/JPEG/WebP use the same local validation and downsizing as Image backgrounds, with a 12 MB limit. Pictures stay in IndexedDB on this device and use the same dots, palette and reduced-motion treatment as built-in art.

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
