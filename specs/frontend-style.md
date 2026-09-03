# Frontend Style

Last updated: 2026-08-31

## Scope

`static/css/style.css`, every file under `static/js/`. Read this before adding any new UI — for us, and for anyone forking or branching this repo later.

## The rule

**Never hand-roll a one-off look.** JARVIS has one design system, defined once in `static/css/style.css`, and every screen (Chats, Calendar, Tasks, Settings, onboarding) builds on the same handful of primitives. Adding a new panel, button, input, or dropdown means reusing an existing class or CSS variable — not writing new inline styles that happen to look close. A UI that's 90% consistent reads as broken; the whole point of a design system is that nothing has to be checked by eye against its neighbors.

If you genuinely need something the system doesn't have yet (a new component shape, not just a new instance of an existing one), add it to `style.css` as a real, named, reusable rule — the same way `.glass`, `.bracket`, and `.btn` were added — not as a `style="..."` attribute buried in one view file.

## The system, in one read

`style.css`'s top comment names it: **"glossy and glassy"** — synthesized from David's own wireframe (structure), Odysseus's per-tab layout patterns (`~/odysseus`, referenced throughout this build — see [[JARVIS Plan]] for specific pulls), and The Bridge's existing tactical-HUD skin (floating glass panels, bracket corners, cyan accent). Dark mode only for now; colors live in CSS custom properties specifically so a future `[data-theme="light"]` override is additive, not a rewrite.

### Tokens (`:root` in `style.css`)

Always reach for these instead of a hardcoded color/radius/shadow:

- `--bg`, `--bg-panel`, `--bg-panel-solid` — background layers.
- `--border`, `--border-strong` — cyan-tinted borders, two intensities.
- `--accent` (`#00d4ff`) — the one accent color. Don't introduce a second accent hue.
- `--text`, `--text-dim`, `--text-faint` — three text intensities, not arbitrary grays.
- `--danger` — the one destructive-action color (`.btn.danger`, error text).
- `--radius` — the one corner radius (`12px`) for panels.
- `--glass-sheen`, `--shadow-elevated`, `--shadow-lifted` — the glass panel's diagonal highlight and layered elevation shadows.

### Primitives

- **`.glass`** — the translucent blurred panel every card/modal sits on. Apply it, don't reinvent `backdrop-filter`/`box-shadow` combos per component.
- **`.bracket`** — the HUD-style corner brackets (top-left/bottom-right), stackable with `.glass` (`class="glass bracket card"` is the common combo, used for onboarding cards, modals, etc.).
- **`.btn`** / **`.btn.danger`** — every clickable action. Don't style a raw `<button>` by hand.
- **`.card`** — padded content block inside a panel (`.title`/`.meta` for heading/subtext).
- **`input`, `textarea`, `select`** — styled as one group at the top of `style.css`, so a new form field matches automatically just by using the plain tag. `select` additionally gets a custom cyan chevron (`appearance: none` + an inline SVG background-image, since a native OS caret doesn't match the glass/cyan look and `currentColor` can't reach into a CSS `background-image`) — added 2026-08-31 after the API-model provider dropdown shipped looking inconsistent with everything else. If you add a *styled* dropdown that isn't a plain `<select>` (e.g. a custom combo/menu like the Integrations "+ Add Integration" button+menu pattern in `settings.js`), it still needs to reuse `.btn`/`.glass`/the color tokens — see `.overflow-menu` in `style.css` for the existing pattern.
- **Icons** — hand-written stroke SVGs (`viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"`), never an icon font or CDN. See `.nav-item svg`, `.sidebar-settings-btn svg`, or any icon in `static/js/icons.js` for the exact shape. `currentColor` means the same SVG automatically re-colors with its container's `color` — don't hardcode a fill/stroke color inside an icon's `<svg>` unless it truly must stay one color everywhere (the `select` chevron above is the one exception, and the comment there explains why).

### Building elements

Every view builds DOM through `el(tag, attrs, children)` in `static/js/api.js` — not template strings, not raw `innerHTML` for anything with event handlers. Look at any existing view (`static/js/views/settings.js` is the largest and most representative) before adding a new one, and match its structure: one `render*Panel(content)` function per screen section, `api()` from `api.js` for every backend call, `el()` for every node.

## When you're not sure

Check how an existing, shipped screen solved the same kind of problem before inventing a new pattern — Settings alone has examples of cards, forms, dropdowns, toggles, tables, and modals. If nothing in this repo has the shape you need, the second reference is `~/odysseus` (a separate local clone, not part of this repo) — several existing patterns here (the Integrations panel, the API-model provider dropdown, the Tasks premade-action gallery) were built by reading its real UI first. See [[JARVIS Plan]] in the vault for the specific features that came from there and why.
