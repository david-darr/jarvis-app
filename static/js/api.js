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

// Custom dropdown, drop-in replacement for el("select", attrs, optionEls).
// Real bug found live 2026-09-01 (David sent a screenshot): a native
// <select>'s open option-list is OS/Chromium-native chrome that CSS can't
// reliably restyle — `color-scheme: dark` on :root (added earlier believing
// it fixed this) does NOT make Electron's popup honor the app's dark theme,
// proven by the screenshot showing a plain white list. Every other custom
// menu in the app (.model-picker-menu, .overflow-menu) is a real styled DOM
// node instead of relying on native chrome, so this does the same: reads
// value/text/selected/disabled off the same <option> elements callers
// already build, and exposes a compatible-enough surface (.value getter/
// setter, .disabled, real "change" events) that no call site needs a
// different shape, just el("select", ...) swapped for customSelect(...).
export function customSelect(attrs = {}, optionEls = []) {
  const opts = [].concat(optionEls).filter(Boolean).map((o) => ({
    value: o.getAttribute("value") ?? "",
    text: o.textContent,
    disabled: o.hasAttribute("disabled"),
    initiallySelected: o.hasAttribute("selected"),
  }));
  let current = (opts.find((o) => o.initiallySelected) || opts[0] || { value: "" }).value;

  const btn = el("button", { type: "button", class: "custom-select-btn" });
  const label = el("span", { class: "custom-select-label" });
  btn.append(label, el("span", { class: "custom-select-chevron" }));
  // Menu is appended to <body> (not wrap) and positioned with `fixed`
  // coordinates computed from the button's real rect on open — a real bug
  // found live 2026-09-01 (David sent a screenshot): an `absolute`-positioned
  // menu nested inside the wrap can only paint above elements in its own
  // stacking context, and a `.glass.bracket.card` sibling further down the
  // page creates its own stacking context (backdrop-filter), so it painted
  // over the menu regardless of z-index. Anchoring to body sidesteps every
  // ancestor's stacking context and overflow:hidden/auto clipping for good,
  // not just for this one card layout.
  const menu = el("div", { class: "custom-select-menu hidden" });
  document.body.appendChild(menu);
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
  function closeMenu() { menu.classList.add("hidden"); }
  function positionMenu() {
    const rect = btn.getBoundingClientRect();
    menu.style.left = `${rect.left}px`;
    menu.style.top = `${rect.bottom + 6}px`;
    menu.style.width = `${rect.width}px`;
  }
  function openMenu() {
    document.querySelectorAll(".custom-select-menu").forEach((m) => m.classList.add("hidden"));
    menu.innerHTML = "";
    for (const o of opts) {
      const item = el("button", {
        type: "button",
        class: "custom-select-item" + (o.value === current ? " active" : "") + (o.disabled ? " disabled" : ""),
        text: o.text,
      });
      if (!o.disabled) {
        item.addEventListener("click", (e) => {
          e.stopPropagation();
          current = o.value;
          syncLabel();
          closeMenu();
          wrap.dispatchEvent(new Event("change"));
        });
      }
      menu.appendChild(item);
    }
    positionMenu();
    menu.classList.remove("hidden");
  }
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    if (wrap.disabled) return;
    if (menu.classList.contains("hidden")) openMenu(); else closeMenu();
  });
  document.addEventListener("click", closeMenu);
  window.addEventListener("scroll", closeMenu, true);
  window.addEventListener("resize", closeMenu);
  // The menu is a detached body child, not a DOM descendant of wrap — clean
  // it up when wrap itself is removed (e.g. a Discord bot card re-rendered
  // after Save/Remove), otherwise it'd leak a hidden menu node per rebuild.
  //
  // `hasBeenMounted` is the whole point: "wrap isn't in the document" is true
  // for a select that's been REMOVED, but equally true for one that hasn't
  // been INSERTED yet — and every view here builds its form first and appends
  // the container last. Without this guard the observer fired on an unrelated
  // body mutation, deleted the menu of a select that was still being built,
  // and disconnected. The button then opened a node that was no longer in the
  // document, so the dropdown looked dead: it clicked, and nothing appeared.
  // (David, 2026-09-04: the Tasks tab's Schedule dropdown — 6 selects on the
  // page, 1 surviving menu.)
  let hasBeenMounted = false;
  const cleanupObserver = new MutationObserver(() => {
    if (wrap.isConnected) { hasBeenMounted = true; return; }
    if (!hasBeenMounted) return; // built, not yet inserted — not garbage
    menu.remove();
    cleanupObserver.disconnect();
  });
  cleanupObserver.observe(document.body, { childList: true, subtree: true });

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
