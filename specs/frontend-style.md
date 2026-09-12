# Frontend Style

Last updated: 2026-09-12

## Direction

Quiet intelligence: near-black surfaces, generous space, clear hierarchy, restrained violet focus, and expressive color reserved for the core and vault. No tactical-HUD brackets, neon outlines, glass-card stacks, or whole-app recoloring in Developer Mode.

References: Linear's workspace structure; Monopo's light typography and negative space; Capy/Claude/ChatGPT's calm conversation surfaces; Spell UI's restrained motion; Dala's colored triangular particles; [Lab01](https://lab01.dev/)'s UI experiments as a reference for compact controls and deliberate transitions. The liquid-metal cue informs the composer's subtle silver sheen, not a full metallic theme.

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

### Collapsible sidebar

The header's panel toggle collapses the desktop sidebar from `--sidebar-width: 204px` to `--sidebar-rail-width: 72px` over 260ms using `--ease-out`. The content naturally takes the released space; do not overlay the rail or remount the current view. Labels fade while icons remain usable. Account, Settings, and Developer Mode remain accessible in the rail.

`sidebar.js` owns the local `jarvis:sidebar-collapsed` preference, toggle state, and hover/focus tooltips. Storage failure must not prevent toggling. All navigation buttons have accessible names, custom tabs without artwork get a fallback icon, and the toggle exposes `aria-expanded`. Tooltips render outside the scrolling sidebar so they cannot be clipped. At 768px and below the desktop preference has no visual effect: keep the full labeled mobile drawer. Reduced motion removes the transition.

## Home

An editorial hero pairs the conversation action with a particle core. A summary strip and two-column workspace expose conversations, open notes, enabled automations, the next seven days, projects, models, recent activity, and connected systems. On phones these stack in normal flow.

Summary requests are read-only; missing data gets an unavailable state, not a fabricated zero. Home links use `jarvis:navigate` with a real session/project/Brain section when applicable. Date-only calendar entries are local days. Clear countdown and refresh timers when leaving the view.

## Chat and motion

A centered reading column uses compact user bubbles, unboxed assistant messages, and a bottom composer with attachment/model controls. Starter chips draft text only; they never send a message.

The `.border-beam` composer implements the requested Libraries.dev-style border effect natively, without adding a React wrapper to this non-React app. A masked conic gradient animates a registered CSS angle around the border at 0.7 opacity, with a restrained focus highlight. It must not intercept input or clip the model menu. It pauses while the document is hidden; reduced-motion makes it static. This is not the border-beam npm package and does not expose its React props.

`core3d.js` renders locally batched triangular particles with Three.js. It reacts to actual in-flight conversations and system status, supports pause, respects reduced motion/visibility, and disposes GPU resources on unmount. A static fallback covers unavailable WebGL.

The vault uses colored note triangles, folder circles, faint edges, and selective labels. Search and Browse vault provide keyboard-accessible alternatives to canvas interaction. It fits while settling, yields camera control when explored, and stops its simulation when settled. Preserve drag, zoom, browse, read, and edit behavior.

## Lifecycle and verification

Each navigation owns a fresh root; delayed work must not overwrite a newer view. Resource-owning views return cleanup functions for listeners, timers, observers, and graphics. Home returns cleanup synchronously while requests are pending.

Run `electron/node_modules/electron/dist/electron.exe scripts/ui-smoke.cjs` for isolated renderer checks. It serves synthetic fixtures on a temporary loopback port, blocks other network requests, and never contacts the live backend. Screenshots and a JSON result go into a unique directory under ignored `data/ui-review/`. Check desktop/mobile layouts, empty/error states, navigation, vault search/read, and composer interactions. These checks do not replace David's review with real account data or authorize a build, commit, or deployment.

## Website and GitHub imagery

`docs/index.html`, `docs/style.css`, and `docs/site.js` are the static public website, with no build step or external font/runtime dependency. Match the application's neutral surfaces, light primary buttons, quiet typography, subtle borders, and motion preferences. Monospace is limited to short section labels. Preserve the download, source, and installation links; do not imply the development UI is already in the packaged release.

The four-button preview switches between Home, Chat, Vault, and the collapsed sidebar. Use real buttons with a pressed state, keep the image's alternative text and full-size link synchronized, and keep the default screenshot usable without JavaScript. Maintain mobile navigation, keyboard focus, a skip link, image dimensions, and reduced-motion behavior.

The website and root README share `docs/img/` screenshots. They are actual app renders with synthetic fixture data, not personal account captures or generated mockups. Captions must say development preview/sample data. Keep the existing logo/favicon; replacing the brand is not part of a screenshot refresh.

Refresh the ten product images only with the explicit `--update-doc-images` flag on the UI smoke command. The runner verifies sidebar animation, keyboard operation, persisted state, mobile override, and website previews/images/layout, alongside the existing UI checks. Review the generated desktop/mobile website and app screenshots before handoff. The flag copies only the named product captures into `docs/img/`; a normal test run leaves tracked images alone. Updating these files locally does not publish the website or push to GitHub.

For hidden Electron tests, enable DevTools focus emulation after loading a page and dispatch keyboard input through DevTools. Native-window input does not reliably reach an offscreen window. Wait for width transitions after restoring preferences, and clear test-only focus/scroll positions before taking publication screenshots.
