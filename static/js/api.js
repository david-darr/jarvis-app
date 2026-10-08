// Thin fetch wrapper — every view module goes through this rather than
// calling fetch() directly, matching Odysseus's own shared-request-helper
// pattern (see specs/frontend.md's appConfig.js).

export async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail || detail;
    } catch (_) {}
    // Auto-toast failed mutations only (David's ask 2026-09-02: background/
    // action failures were invisible). GETs stay silent on purpose — views
    // handle their own empty/fallback states (Home alone fires 7 GETs with
    // intentional .catch(() => []) fallbacks; toasting those would spam 7
    // error toasts the moment the backend hiccups).
    const method = (options.method || "GET").toUpperCase();
    if (method !== "GET") toast(`${detail}`, "error");
    throw new Error(`${res.status}: ${detail}`);
  }
  if (res.status === 204) return null;
  const contentType = res.headers.get("content-type") || "";
  if (contentType.includes("application/json")) return res.json();
  return res;
}

// -- toast notifications (David's ask 2026-09-02, proper-user-ready-app
// polish) — small glass slide-up cards, bottom-right, auto-dismissing.
// Views can call toast() directly for high-value confirmations; api()
// above fires the error variant automatically on any failed mutation.
const TOAST_MS = 4200;

export function toast(message, type = "info") {
  let host = document.getElementById("toast-host");
  if (!host) {
    host = el("div", { id: "toast-host" });
    document.body.appendChild(host);
  }
  const node = el("div", { class: `toast ${type}`, text: message });
  host.appendChild(node);
  // Force a layout so the transition actually animates from the initial state.
  node.getBoundingClientRect();
  node.classList.add("show");
  const dismiss = () => {
    node.classList.remove("show");
    node.addEventListener("transitionend", () => node.remove(), { once: true });
    // Fallback removal in case transitionend never fires (display:none tab).
    setTimeout(() => node.remove(), 600);
  };
  const timer = setTimeout(dismiss, TOAST_MS);
  node.addEventListener("click", () => { clearTimeout(timer); dismiss(); });
  return node;
}

export function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "text") node.textContent = value;
    else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
    else if (typeof value === "boolean") {
      // Found live 2026-09-10: boolean HTML attributes (disabled, checked,
      // required, ...) are governed by the attribute's PRESENCE, not its
      // string content -- setAttribute("disabled", false) still writes
      // disabled="false", which every browser treats as disabled. That
      // silently made `disabled: !allReady`-style props permanently true
      // regardless of the actual condition (Settings > Remote Access'
      // "Turn on remote access" button, found stuck grayed-out on a fully
      // ready install; cookbook.js's "Use in Chat" button has the same
      // pattern and was very likely broken the same way, just unnoticed).
      // Assign the real IDL property when one exists on the node (the
      // normal case for every genuine boolean attribute -- correctly
      // coerces true/false), otherwise fall back to real presence/absence.
      if (key in node) node[key] = value;
      else if (value) node.setAttribute(key, "");
      else node.removeAttribute(key);
    }
    else node.setAttribute(key, value);
  }
  for (const child of [].concat(children)) {
    if (child) node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
  }
  return node;
}

// -- confirm dialog (David's ask 2026-09-03) ------------------------------
// Every destructive action in the app used to fire immediately on a single
// click with no undo — deleting a note, an email account, a document, a
// skill, a calendar event, or an entire chat conversation. This is the one
// shared gate for all of them. Returns a Promise<boolean>; resolves false
// on cancel, backdrop click, or Escape.
//
// Deliberately built on the same .modal-backdrop/.modal-panel primitives as
// the workspace picker and Calendar's archive rather than window.confirm() —
// a native confirm is unstyleable OS chrome that breaks the glass look, the
// same reason customSelect() exists (see its comment above).
export function confirmDialog({ title, message, confirmLabel = "Delete", danger = true }) {
  return new Promise((resolve) => {
    const cancelBtn = el("button", { class: "btn", text: "Cancel" });
    const confirmBtn = el("button", { class: danger ? "btn danger" : "btn", text: confirmLabel });
    const panel = el("div", { class: "glass modal-panel confirm-panel" }, [
      el("h4", { text: title }),
      el("div", { class: "muted", text: message }),
      el("div", { class: "modal-footer" }, [cancelBtn, confirmBtn]),
    ]);
    const backdrop = el("div", { class: "modal-backdrop" }, [panel]);
    panel.addEventListener("click", (e) => e.stopPropagation());

    let settled = false;
    const finish = (result) => {
      if (settled) return;
      settled = true;
      document.removeEventListener("keydown", onKey);
      backdrop.remove();
      resolve(result);
    };
    const onKey = (e) => {
      if (e.key === "Escape") { e.preventDefault(); finish(false); }
      else if (e.key === "Enter") { e.preventDefault(); finish(true); }
    };

    backdrop.addEventListener("click", () => finish(false));
    cancelBtn.addEventListener("click", () => finish(false));
    confirmBtn.addEventListener("click", () => finish(true));
    document.addEventListener("keydown", onKey);

    document.body.appendChild(backdrop);
    confirmBtn.focus();
  });
}

// -- row action icon button ------------------------------------------------
// Replaces the repeated full-text "Delete"/"Remove" buttons that used to sit
// in every list row (David's ask 2026-09-03). Same hover-reveal idea chat.js
// already used for .msg-actions, generalized: the row gets .has-row-actions,
// the buttons fade in on hover/focus-within so a list reads as content
// first, controls second.
export function iconButton(iconSvg, title, onclick, { danger = false } = {}) {
  const btn = el("button", {
    type: "button",
    class: "row-action-btn" + (danger ? " danger" : ""),
    title,
    "aria-label": title,
    onclick,
  });
  btn.insertAdjacentHTML("beforeend", iconSvg);
  return btn;
}

// -- empty state -----------------------------------------------------------
// Bare "Nothing here yet." sentences replaced with a real icon + hint, and
// optionally a primary action so an empty list is a starting point rather
// than a dead end (David's ask 2026-09-03).
// A model provider's logo (static/img/model-marks/, LobeHub, MIT), or null when
// core/model_marks.py found none and the caller keeps its generic icon. Drawn as a
// mask in the current text colour, so it follows the theme; `mark` only ever comes
// from the server's fixed list, and is checked here anyway before it reaches a URL.
export function modelMark(mark, label = "") {
  if (!mark || !/^[a-z0-9-]+$/.test(mark)) return null;
  const node = el("span", { class: "model-mark", role: "img", "aria-label": label || mark });
  node.style.setProperty("--mark", `url('/static/img/model-marks/${mark}.svg')`);
  return node;
}

export function emptyState({ icon, title, hint, actionLabel, onAction }) {
  const node = el("div", { class: "empty-state empty-state-rich" });
  if (icon) {
    const iconHost = el("div", { class: "empty-state-icon" });
    iconHost.insertAdjacentHTML("beforeend", icon);
    node.appendChild(iconHost);
  }
  node.appendChild(el("div", { class: "empty-state-title", text: title }));
  if (hint) node.appendChild(el("div", { class: "empty-state-hint", text: hint }));
  if (actionLabel && onAction) {
    node.appendChild(el("button", { class: "btn", style: "margin-top:14px;", text: actionLabel, onclick: onAction }));
  }
  return node;
}

// One option menu for every dropdown in the app: customSelect()'s, and every
// native <select>'s (useAppMenusForSelects below). A native <select>'s open
// list is drawn by the operating system, which CSS can't restyle (David,
// 2026-09-01 and 2026-10-07: a plain white Windows list), so the list is a
// real styled node instead. It lives on <body> with `fixed` coordinates from
// the control's rect, so no ancestor's stacking context or overflow can clip
// it, and it exists only while open.
let openMenu = null;

export function closeOptionMenu() {
  if (!openMenu) return;
  const { menu, cleanup } = openMenu;
  openMenu = null;
  cleanup();
  menu.remove();
}

// items: [{ value, text, selected, disabled } | { heading }]
export function openOptionMenu(anchor, items, onPick) {
  closeOptionMenu();
  const menu = el("div", { class: "custom-select-menu", role: "listbox" });
  for (const item of items) {
    if (item.heading !== undefined) { menu.append(el("div", { class: "custom-select-heading", text: item.heading })); continue; }
    const row = el("button", {
      type: "button", role: "option", "aria-selected": String(!!item.selected),
      class: "custom-select-item" + (item.selected ? " active" : "") + (item.disabled ? " disabled" : ""),
      text: item.text,
    });
    if (item.disabled) row.disabled = true;
    // Focus goes back to the control without a keyboard ring after a click.
    else row.addEventListener("click", (e) => { e.stopPropagation(); closeOptionMenu(); anchor.focus?.({ focusVisible: false }); onPick(item.value); });
    menu.append(row);
  }
  document.body.append(menu);

  // Below the control, or above it when there is more room there, never
  // taller than the space on that side (2026-10-05: a long list near the
  // bottom of Settings ran off the window). From the chat composer it opens
  // above the whole composer, like its other menus.
  const GAP = 6, MARGIN = 12, MAX = 260;
  const rect = anchor.getBoundingClientRect();
  const composer = anchor.closest(".chat-input-bar")?.getBoundingClientRect();
  const top = composer ? composer.top - 2 : rect.top;
  const below = window.innerHeight - rect.bottom - GAP - MARGIN;
  const above = top - GAP - MARGIN;
  const up = composer ? true : below < Math.min(MAX, menu.scrollHeight) && above > below;
  menu.style.maxHeight = `${Math.max(120, Math.min(MAX, up ? above : below))}px`;
  const width = Math.max(rect.width, 160);
  menu.style.width = `${width}px`;
  menu.style.left = `${Math.max(MARGIN, Math.min(rect.left, window.innerWidth - width - MARGIN))}px`;
  menu.style.top = up ? "" : `${rect.bottom + GAP}px`;
  menu.style.bottom = up ? `${window.innerHeight - top + GAP}px` : "";
  (menu.querySelector(".custom-select-item.active") || menu.querySelector(".custom-select-item:not(.disabled)"))?.scrollIntoView({ block: "nearest" });

  const rows = () => [...menu.querySelectorAll(".custom-select-item:not(.disabled)")];
  const onKey = (e) => {
    // Capture phase, so Escape closes this menu and not also the Settings
    // window or dialog behind it.
    if (e.key === "Escape") { e.preventDefault(); e.stopImmediatePropagation(); closeOptionMenu(); anchor.focus?.(); return; }
    if (e.key === "Tab") { closeOptionMenu(); return; }
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    e.preventDefault(); e.stopImmediatePropagation();
    const list = rows();
    if (!list.length) return;
    const at = list.indexOf(document.activeElement);
    const start = at < 0 ? list.findIndex((r) => r.classList.contains("active")) : at;
    const next = e.key === "ArrowDown" ? Math.min(list.length - 1, start + 1) : Math.max(0, start < 0 ? 0 : start - 1);
    list[next].focus();
  };
  const onClick = (e) => { if (!menu.contains(e.target) && !anchor.contains(e.target)) closeOptionMenu(); };
  // Scrolling the page moves the control out from under a fixed menu, so it
  // closes, but not when the scroll is the menu's own list (2026-10-05: the
  // 18-platform list in Settings closed the moment it was scrolled).
  const onScroll = (e) => { if (!menu.contains(e.target)) closeOptionMenu(); };
  document.addEventListener("keydown", onKey, true);
  document.addEventListener("click", onClick, true);
  window.addEventListener("scroll", onScroll, true);
  window.addEventListener("resize", closeOptionMenu);
  openMenu = { menu, anchor, cleanup: () => {
    document.removeEventListener("keydown", onKey, true);
    document.removeEventListener("click", onClick, true);
    window.removeEventListener("scroll", onScroll, true);
    window.removeEventListener("resize", closeOptionMenu);
  } };
  return menu;
}

const isOpenFor = (anchor) => openMenu?.anchor === anchor;

// Every native <select> opens the app's menu instead of the system's list.
// The <select> stays the real control: its look, value, form name and
// "change" events are unchanged; only the list it opens is replaced, read
// fresh from its options each time, so lists filled in later just work.
// Phones keep their own picker, which suits touch better. Called once at
// startup; it covers selects added later too, custom tabs included.
export function useAppMenusForSelects() {
  if (window.matchMedia?.("(pointer: coarse)").matches) return;
  const usable = (t) => t instanceof HTMLSelectElement && !t.multiple && t.size <= 1 && !t.disabled;
  const option = (o) => ({ value: o.value, text: o.label || o.textContent, selected: o.selected, disabled: o.disabled, hidden: o.hidden });
  const open = (select) => {
    if (isOpenFor(select)) { closeOptionMenu(); return; }
    const items = [];
    for (const child of select.children) {
      if (child instanceof HTMLOptGroupElement) items.push({ heading: child.label }, ...[...child.children].map(option));
      else if (child instanceof HTMLOptionElement) items.push(option(child));
    }
    openOptionMenu(select, items.filter((i) => !i.hidden), (value) => {
      if (select.value === value) return;
      select.value = value;
      select.dispatchEvent(new Event("input", { bubbles: true }));
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });
  };
  document.addEventListener("mousedown", (e) => {
    if (e.button !== 0 || !usable(e.target)) return;
    e.preventDefault();  // stops the system list from opening
    e.target.focus({ focusVisible: false });
    open(e.target);
  }, true);
  // The keys that would open the system list open this one; plain arrow
  // keys still step through the options in place, as before.
  document.addEventListener("keydown", (e) => {
    if (!usable(e.target)) return;
    if (e.key === "Enter" || e.key === " " || e.key === "F4" || (e.altKey && (e.key === "ArrowDown" || e.key === "ArrowUp"))) {
      e.preventDefault();
      open(e.target);
    }
  }, true);
}

// Custom dropdown, drop-in replacement for el("select", attrs, optionEls):
// reads value/text/selected/disabled off the same <option> elements callers
// already build, and exposes a compatible-enough surface (.value getter/
// setter, .disabled, real "change" events) that no call site needs a
// different shape. Its list is the shared option menu above.
export function customSelect(attrs = {}, optionEls = []) {
  const opts = [].concat(optionEls).filter(Boolean).map((o) => ({
    value: o.getAttribute("value") ?? "",
    text: o.textContent,
    disabled: o.hasAttribute("disabled"),
    initiallySelected: o.hasAttribute("selected"),
  }));
  let current = (opts.find((o) => o.initiallySelected) || opts[0] || { value: "" }).value;

  const btn = el("button", { type: "button", class: "custom-select-btn", "aria-haspopup": "listbox" });
  const label = el("span", { class: "custom-select-label" });
  btn.append(label, el("span", { class: "custom-select-chevron" }));
  const wrap = el("div", { class: "custom-select" });
  wrap.append(btn);

  if (attrs.style) wrap.setAttribute("style", attrs.style);
  if (attrs.class) wrap.classList.add(...attrs.class.split(/\s+/));
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "style" || key === "class" || key === "disabled") continue;
    if (key.startsWith("on") && typeof value === "function") wrap.addEventListener(key.slice(2), value);
  }

  function syncLabel() {
    const match = opts.find((o) => o.value === current);
    label.textContent = match ? match.text : "";
  }
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    if (wrap.disabled) return;
    if (isOpenFor(btn)) { closeOptionMenu(); return; }
    openOptionMenu(btn, opts.map((o) => ({ value: o.value, text: o.text, disabled: o.disabled, selected: o.value === current })), (value) => {
      current = value;
      syncLabel();
      wrap.dispatchEvent(new Event("change"));
    });
  });

  Object.defineProperty(wrap, "value", {
    get() { return current; },
    set(v) { current = v; syncLabel(); },
  });
  Object.defineProperty(wrap, "disabled", {
    get() { return btn.disabled; },
    set(v) { btn.disabled = !!v; wrap.classList.toggle("disabled", !!v); },
  });
  if (attrs.disabled) wrap.disabled = true;

  syncLabel();
  return wrap;
}
