import { el } from "./api.js";

// The building blocks every Settings page is made of (redesign 2026-10-05,
// after Hermes's desktop settings, Codex and Claude): a page header, quiet
// groups of rows with hairline dividers, and one row shape - label and a
// one-line description on the left, the control on the right, stacking under
// the label when the pane is narrow. Pages compose these instead of
// hand-rolling cards, so Settings reads as one product.

export function pageHeader(title, description, actions = []) {
  return el("header", { class: "set-header" }, [
    el("div", { class: "set-header-text" }, [
      el("h2", { class: "set-title", text: title }),
      description ? el("p", { class: "set-description", text: description }) : null,
    ]),
    actions.length ? el("div", { class: "set-header-actions" }, actions) : null,
  ]);
}

// A titled block of rows. `title` and `actions` are optional; rows can be any
// element, though they are usually row().
export function group({ title, description, actions = [], cls = "" } = {}, rows = []) {
  const body = el("div", { class: "set-group" }, rows.filter(Boolean));
  const head = title || actions.length ? el("div", { class: "set-section-head" }, [
    el("div", {}, [
      title ? el("h3", { class: "set-section-title", text: title }) : null,
      description ? el("p", { class: "set-section-description", text: description }) : null,
    ]),
    actions.length ? el("div", { class: "set-section-actions" }, actions) : null,
  ]) : null;
  const section = el("section", { class: `set-section ${cls}`.trim() }, [head, body]);
  section.body = body;
  return section;
}

// The one settings row. `stack` puts the control under the description at
// any width (long inputs, textareas); `below` is extra content under the
// label, such as a status line or a nested list. `onOpen` makes the whole
// row a link (by click or Enter) except where a control inside was used.
export function row({ title, description, control, below, stack = false, icon, cls = "", attrs = {} }, onOpen) {
  const controls = [].concat(control || []).filter(Boolean);
  const node = el("div", { class: `set-row${stack ? " set-row-stack" : ""}${onOpen ? " is-link" : ""} ${cls}`.trim(), ...attrs }, [
    icon || null,
    el("div", { class: "set-row-text" }, [
      typeof title === "string" ? el("div", { class: "set-row-title", text: title }) : el("div", { class: "set-row-title" }, [].concat(title)),
      description ? (typeof description === "string"
        ? el("div", { class: "set-row-description", text: description })
        : el("div", { class: "set-row-description" }, [].concat(description))) : null,
      ...[].concat(below || []),
    ]),
    controls.length ? el("div", { class: "set-row-control" }, controls) : null,
  ]);
  if (onOpen) {
    if (!node.hasAttribute("tabindex")) node.tabIndex = 0;
    node.setAttribute("role", "button");
    const used = (target) => target.closest("button, a, input, textarea, select, .custom-select") && !target.classList.contains("set-chevron");
    node.addEventListener("click", (event) => { if (!used(event.target)) onOpen(); });
    node.addEventListener("keydown", (event) => { if (event.key === "Enter" && event.target === node) onOpen(); });
  }
  return node;
}

// A field row: a label and an input, the input on the right (or under the
// label on a narrow pane). For forms that add or edit something.
export function field(label, input, help) {
  return row({ title: label, description: help, control: input, cls: "set-field" });
}

// Every on/off setting is a switch. `onChange(on)` may be async; the switch
// shows the new state at once and goes back if it throws.
export function toggle({ checked = false, onChange, label = "", disabled = false } = {}) {
  const button = el("button", { type: "button", role: "switch", class: "set-switch", "aria-label": label,
    "aria-checked": String(!!checked) });
  button.disabled = disabled;
  button.addEventListener("click", async () => {
    const next = button.getAttribute("aria-checked") !== "true";
    button.setAttribute("aria-checked", String(next));
    try { await onChange?.(next); } catch { button.setAttribute("aria-checked", String(!next)); }
  });
  Object.defineProperty(button, "checked", {
    get: () => button.getAttribute("aria-checked") === "true",
    set: (value) => button.setAttribute("aria-checked", String(!!value)),
  });
  return button;
}

// tone: ok | warn | error | muted | accent
export function pill(text, tone = "muted") {
  return el("span", { class: `set-pill set-pill-${tone}`, text });
}

export function note(text, cls = "") {
  return el("p", { class: `set-note ${cls}`.trim() }, [].concat(text));
}

export function empty(text) {
  return el("div", { class: "set-empty", text });
}

// A round initial for a platform, account or person, in place of brand logos
// we do not ship.
export function badge(text, hue = 0) {
  return el("span", { class: "set-badge", "aria-hidden": "true", text: (text || "?").slice(0, 1).toUpperCase(),
    style: `--badge-hue:${hue}` });
}

export function hueFor(text) {
  let hash = 0;
  for (const ch of String(text || "")) hash = (hash * 31 + ch.charCodeAt(0)) % 360;
  return hash;
}
