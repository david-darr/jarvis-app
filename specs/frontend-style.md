# Frontend Style

Last updated: 2026-09-15

## Direction

Quiet intelligence: near-black surfaces, generous space, clear hierarchy, restrained violet focus, and expressive color reserved for the core and vault. No tactical-HUD brackets, neon outlines, glass-card stacks, or whole-app recoloring in Developer Mode.

References: Linear's workspace structure; Monopo's light typography and negative space; Capy/Claude/ChatGPT's calm conversation surfaces; Spell UI's restrained motion; Dala's colored triangular particles; [Lab01](https://lab01.dev/)'s UI experiments as a reference for compact controls and deliberate transitions. The liquid-metal cue informs the composer's subtle silver sheen, not a full metallic theme.

The chat-focused pass draws on [Zeron](https://github.com/zeronsh/zeron): minimal navigation, restrained header controls, soft user bubbles, and an uncluttered conversation canvas. [Libraries.dev](https://libraries.dev/) informs the border beam; [Obsidian UI](https://www.obsidianui.dev/) informs quiet hover/selection feedback. These are visual references, not copied application code or added React dependencies. Local development changes remain pending David's visual review.

## One shared system

Scope: `static/css/style.css` and `static/js/`. Reuse shared classes and tokens rather than introducing inline colors, shadows, or a one-off look. Add a named reusable component when the system genuinely lacks a shape.

The renderer is native JavaScript, not React. Views build DOM with `el()` and access the backend through `api()` from `static/js/api.js`. Preserve API contracts, authentication, streaming, and actions. Never substitute sample counts or simulated activity for live data in application views.

### Tokens

- `--bg: #101113`: workspace background.
- `--bg-panel: #17181b`, `--bg-panel-solid: #1c1d21`, `--surface-2: #222328`: solid surface layers.
- `--border`, `--border-strong`: neutral white at 8% and 17% opacity.
- `--text: #eeedf0`, `--text-dim: #b0afb8`, `--text-faint: #92919c`: text hierarchy.
- `--accent: #b3a7f5`, `--accent-dim`: restrained focus, links, selection.
- `--danger`, `--success`: meaningful error/destructive and healthy states, paired with labels.
- `--radius: 12px`, `--sidebar-width: 204px`, `--chat-column: 780px`.
- `--shadow-elevated`, `--shadow-lifted`: reserved for floating menus/modals.

Dark mode only. New themes should override tokens, not rewrite components. Typography uses Inter when locally available, then Segoe UI/system sans; no required remote fonts.

### Primitives and layout

- `.glass`: legacy name for a solid, subtly bordered panel. No backdrop blur or decorative corners. `.bracket` remains a compatibility class for existing/custom views.
- `.btn`, `.btn.primary`, `.btn.quiet`, `.btn.danger`: neutral secondary, light primary, text action, destructive action. Use real buttons with accessible names.
- `.card`, `.title`, `.meta`, `.card-row`: shared surfaces and rows.
- `.view-constrained`, `.view-header`: consistent content width and heading. Chat and Calendar have specialized layouts.
- `.disclosure-panel`: native details/summary for secondary creation and connection forms. Main content comes first.
- `.document-grid`/`.document-card`, `.automation-grid`/`.automation-card`: responsive collections.
- Inputs, textareas, selects and `.custom-select`: consistent field and focus treatment. Keep visible labels or accessible names.
- Icons: local stroke SVGs from `icons.js`, using currentColor; no icon fonts/CDNs.

Sidebar groups separate Workspace and Intelligence. On phones it becomes a drawer. Chat has its own history drawer. Settings remains a desktop floating window and mobile full page.

Settings navigation is grouped into Models, Connections, Workspace, Personal, and Administration. Administration is admin-only and Custom Tabs stays behind Developer Mode; grouping changes presentation only and never widens a gate. Its search matches per-section keywords as well as labels, so the words someone actually types find the right panel, and a group heading hides when nothing under it matches. The desktop window is draggable by its titlebar and stays wholly inside the app viewport, re-clamping after a drag, an edge resize, an app resize, and reopening; resize, minimize, and close are unchanged, and the mobile full page has no draggable window.

### Collapsible sidebar

The header's panel toggle collapses the desktop sidebar from `--sidebar-width: 204px` to `--sidebar-rail-width: 52px` over 260ms using `--ease-out`. The content naturally takes the released space; do not overlay the rail or remount the current view. Labels fade while icons remain usable. Account, Settings, and Developer Mode remain accessible in the rail.

`sidebar.js` owns the local `jarvis:sidebar-collapsed` preference, toggle state, and hover/focus tooltips. Storage failure must not prevent toggling. All navigation buttons have accessible names, custom tabs without artwork get a fallback icon, and the toggle exposes `aria-expanded`. Tooltips render outside the scrolling sidebar so they cannot be clipped. At 768px and below the desktop preference has no visual effect: keep the full labeled mobile drawer. Reduced motion removes the transition.

## Home

An editorial hero pairs the conversation action with a particle core. A summary strip and two-column workspace expose conversations, open notes, enabled automations, the next seven days, projects, models, recent activity, and connected systems. On phones these stack in normal flow.

Summary requests are read-only; missing data gets an unavailable state, not a fabricated zero. Home links use `jarvis:navigate` with a real session/project/Brain section when applicable. Date-only calendar entries are local days. Clear countdown and refresh timers when leaving the view.

## Chat and motion

A new chat contains only the centered composer and a quiet header. No welcome artwork, headline, or starter chips. The first message moves the same live composer beneath a centered reading column with soft user bubbles and unboxed assistant responses. Its attachment strip, model controls, and keyboard hint travel together; no cloned input or draft-resetting remount. Layout changes use a 380ms position animation, skipped under reduced motion. A ResizeObserver keeps the Latest button above a growing composer and is disconnected on unmount.

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

Styles live in `static/css/chat.css` and reuse the shared tokens. Preview panels close and release PDF workers when switching sessions or leaving Chat. Escape closes the panel and restores focus. On small screens the preview fills the Chat area.

Rebuild browser dependencies with `npm ci` then `npm run build:chat`. Generated local bundles and third-party license notices under `static/js/vendor/` ship with the static app; end users need neither Node nor a CDN. PDF code loads only when a PDF is opened.

Use `scripts/ui-smoke.cjs --update-chat-image` to refresh only the shared website/GitHub Chat screenshot from the full app with synthetic data. The default test run does not change published image assets.

Run `python scripts/test_chat.py` using the project environment for isolated backend tests, and `electron/node_modules/electron/dist/electron.exe scripts/chat-smoke.cjs` for the dedicated browser checks. Results and screenshots go under ignored `data/chat-review/`. Both suites use synthetic data; they do not verify account-specific model availability or authorize live model calls.

Implementation references: [Codex CLI flags](https://learn.chatgpt.com/docs/developer-commands?surface=cli), [Claude streaming events](https://code.claude.com/docs/en/agent-sdk/streaming-output), [Marked sanitization guidance](https://marked.js.org/), and [PDF.js rendering](https://mozilla.github.io/pdf.js/examples/).

The `.border-beam` composer implements the requested Libraries.dev-style border effect natively, without adding a React wrapper to this non-React app. A masked conic gradient animates a registered CSS angle around the border at 0.4 opacity, rising to 0.7 on focus. It must not intercept input or clip the model menu. It pauses while the document is hidden; reduced-motion makes it static. This is not the border-beam npm package and does not expose its React props.

`core3d.js` renders locally batched triangular particles with Three.js. It reacts to actual in-flight conversations and system status, supports pause, respects reduced motion/visibility, and disposes GPU resources on unmount. A static fallback covers unavailable WebGL.

The vault uses colored note triangles, folder circles, faint edges, and selective labels. Search and Browse vault provide keyboard-accessible alternatives to canvas interaction. It fits while settling, yields camera control when explored, and stops its simulation when settled. Preserve drag, zoom, browse, read, and edit behavior.

## Lifecycle and verification

Each navigation owns a fresh root; delayed work must not overwrite a newer view. Resource-owning views return cleanup functions for listeners, timers, observers, and graphics. Home returns cleanup synchronously while requests are pending.

Run `electron/node_modules/electron/dist/electron.exe scripts/ui-smoke.cjs` for isolated renderer checks. It serves synthetic fixtures on a temporary loopback port, blocks other network requests, and never contacts the live backend. Screenshots and a JSON result go into a unique directory under ignored `data/ui-review/`. Check desktop/mobile layouts, empty/error states, navigation, vault search/read, and composer interactions. These checks do not replace David's review with real account data or authorize a build, commit, or deployment.

## Website and GitHub imagery

`docs/index.html`, `docs/style.css`, and `docs/site.js` are the static public website, with no build step or external font/runtime dependency. Match the application's neutral surfaces, light primary buttons, quiet typography, subtle borders, and motion preferences. Monospace is limited to short section labels. Preserve the download, source, and installation links; do not imply the development UI is already in the packaged release.

The five-button preview switches between Home, New chat, Conversation, Vault, and the collapsed sidebar. Use real buttons with a pressed state, keep the image's alternative text and full-size link synchronized, and keep the default screenshot usable without JavaScript. Maintain mobile navigation, keyboard focus, a skip link, image dimensions, and reduced-motion behavior.

## Appearance (Settings > Personal > Appearance)

Four background modes: Original, Color, Image, Flow. Every setting is a local-device preference, stored per signed-in username in localStorage, with an uploaded image held as a Blob in IndexedDB. Nothing is uploaded and no server setting exists; the panel says so, because "appearance" reading as an account-level setting would be misleading.

The sidebar is always the chosen colour darkened by 18%, and foreground colours flip for light backgrounds so text stays readable against either. Content panels keep solid backgrounds regardless of mode: a shader or photo behind body text is not worth the legibility. Image mode accepts PNG/JPEG/WebP only, up to 12 MB and 40 megapixels, normalised to 2560px on the longest edge and decoded through `createImageBitmap` into a canvas, never assigned as an image URL, so the CSP stays unwidened.

Flow is a native WebGL fragment shader with no added renderer dependency. Distortion, swirl, grain mixer and grain overlay are independent uniforms, and each must visibly change rendered pixels. The canvas caps at 1280px on its longest edge and around 25fps, pauses when the document is hidden, and holds still under reduced motion. **A lost WebGL context must fall back to a static gradient and repaint**, not leave the last frame or an empty canvas behind the whole UI; a restored context returns to the shader. Dials are operable by both pointer drag and keyboard, and the pointer path uses real pointer capture, so it can only be tested with genuine pointer input rather than synthesised events.

## Chat activity

Chat shows an expandable set of transport steps driven by real stream events: sending, connected, waiting, responding, done, interrupted. **It never fabricates reasoning or tool-use steps** — the current backend stream exposes text, done and error only, so anything richer would be invented. The progress element mounts outside the streamed Markdown so token updates never rebuild it, and the floating progress cards keep stable DOM rather than being recreated per token.

## Test commands

Run the Electron suites through `node scripts/run-electron-test.cjs <suite>`. On Windows a direct invocation returns exit 0 while the GUI process is still running, which reads as a pass for a suite that has not finished. The wrapper uses the project's own Electron, waits for the process, passes through output and exit status, and times out at 240 seconds. `browser-smoke --live` adds real websites; `browser-smoke --verify-failure` proves a non-zero exit still survives. Never use npx to fetch a different Electron.

The website and root README share `docs/img/` screenshots. They are actual app renders with synthetic fixture data, not personal account captures or generated mockups. Captions must say development preview/sample data. Keep the existing logo/favicon; replacing the brand is not part of a screenshot refresh.

Refresh the eleven product images only with the explicit `--update-doc-images` flag on the UI smoke command. This includes `chat-new.png` showing the centered composer with both history tucked away and the global rail collapsed. The runner verifies sidebar animation, keyboard operation, persisted state, mobile override, and website previews/images/layout, alongside the existing UI checks. Review the generated desktop/mobile website and app screenshots before handoff. The flag copies only the named product captures into `docs/img/`; a normal test run leaves tracked images alone. Updating these files locally does not publish the website or push to GitHub.

For hidden Electron tests, enable DevTools focus emulation after loading a page and dispatch keyboard input through DevTools. Native-window input does not reliably reach an offscreen window. Wait for width transitions after restoring preferences, and clear test-only focus/scroll positions before taking publication screenshots.
