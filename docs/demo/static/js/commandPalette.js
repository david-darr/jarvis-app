import { api, el, toast } from "./api.js";

// Command palette (Ctrl+K / Cmd+K — David's ask 2026-09-02, keyboard-first
// mission-control feel). Static commands: jump to any tab (built-in +
// custom) and open Settings. Dynamic commands, fetched fresh each open so
// they never go stale: "Run task: X" for every enabled scheduled task.
//
// init() is called once from app.js's startApp() with the app's own
// switchTab/openSettings — the palette stays a dumb launcher over those,
// not a second router.

let deps = null;
let backdrop = null;
let input = null;
let listHost = null;
let commands = [];
let filtered = [];
let activeIndex = 0;

export function init({ nav, customTabs, switchTab, openSettings }) {
  deps = { nav, customTabs, switchTab, openSettings };
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      isOpen() ? close() : open();
    } else if (e.key === "Escape" && isOpen()) {
      close();
    }
  });
}

function isOpen() {
  return backdrop && !backdrop.classList.contains("hidden");
}

async function open() {
  if (!backdrop) build();
  commands = await buildCommands();
  input.value = "";
  applyFilter("");
  backdrop.classList.remove("hidden");
  input.focus();
}

function close() {
  if (backdrop) backdrop.classList.add("hidden");
}

function build() {
  input = el("input", {
    class: "palette-input",
    type: "text",
    placeholder: "Type a command…",
    oninput: () => applyFilter(input.value),
    onkeydown: (e) => {
      if (e.key === "ArrowDown") { e.preventDefault(); moveActive(1); }
      else if (e.key === "ArrowUp") { e.preventDefault(); moveActive(-1); }
      else if (e.key === "Enter") { e.preventDefault(); runActive(); }
    },
  });
  listHost = el("div", { class: "palette-list" });
  const panel = el("div", { class: "glass bracket palette-panel" }, [
    el("div", { class: "palette-input-row" }, [input, el("span", { class: "palette-hint", text: "esc" })]),
    listHost,
  ]);
  panel.addEventListener("click", (e) => e.stopPropagation());
  backdrop = el("div", { class: "palette-backdrop hidden", onclick: close }, [panel]);
  document.body.appendChild(backdrop);
}

async function buildCommands() {
  const cmds = [];
  for (const item of deps.nav) {
    cmds.push({ label: `Go to ${item.label}`, hint: "tab", run: () => deps.switchTab(item.id) });
  }
  for (const item of deps.customTabs) {
    cmds.push({ label: `Go to ${item.label}`, hint: "tab", run: () => deps.switchTab(item.id) });
  }
  cmds.push({ label: "Open Settings", hint: "app", run: () => deps.openSettings() });

  const tasks = await api("/api/tasks").catch(() => []);
  for (const t of tasks.filter((t) => t.enabled)) {
    cmds.push({
      label: `Run task: ${t.name}`,
      hint: "task",
      run: async () => {
        toast(`Running ${t.name}…`, "info");
        const run = await api(`/api/tasks/${t.id}/run`, { method: "POST" }).catch(() => null);
        if (!run) return;
        if (run.error) toast(`${t.name} failed: ${run.error}`, "error");
        else if (run.delivered === false) toast(`${t.name} ran, but delivery failed`, "error");
        else toast(`${t.name} ran${run.delivered ? " and delivered" : ""}`, "success");
      },
    });
  }
  return cmds;
}

function applyFilter(query) {
  const q = query.trim().toLowerCase();
  filtered = q ? commands.filter((c) => c.label.toLowerCase().includes(q)) : commands;
  activeIndex = 0;
  renderList();
}

function moveActive(delta) {
  if (!filtered.length) return;
  activeIndex = (activeIndex + delta + filtered.length) % filtered.length;
  renderList();
}

function runActive() {
  const cmd = filtered[activeIndex];
  if (!cmd) return;
  close();
  cmd.run();
}

function renderList() {
  listHost.innerHTML = "";
  if (!filtered.length) {
    listHost.appendChild(el("div", { class: "empty-state", style: "padding:14px 0;", text: "No matching commands" }));
    return;
  }
  filtered.forEach((cmd, i) => {
    const item = el("button", {
      type: "button",
      class: "palette-item" + (i === activeIndex ? " active" : ""),
      onclick: () => { activeIndex = i; runActive(); },
      onmouseenter: () => { activeIndex = i; renderList(); },
    }, [
      el("span", { class: "palette-item-label", text: cmd.label }),
      el("span", { class: "palette-item-hint", text: cmd.hint }),
    ]);
    listHost.appendChild(item);
  });
  const active = listHost.querySelector(".palette-item.active");
  if (active) active.scrollIntoView({ block: "nearest" });
}
