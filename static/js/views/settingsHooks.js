import { api, el, customSelect, toast, confirmDialog } from "../api.js";
import { group, row, field, toggle, pill, note, empty, badge } from "../settingsKit.js";

// Settings > Administration > Hooks (2026-10-05; services/hook_service.py,
// spec: the vault note "Lifecycle Hooks (Build Spec)"). A hook is a step of
// the person's own that runs on a JARVIS event: post to a web address, add a
// line to a vault note, send to a channel, or run a command on this computer.
// Only a command on "Before a tool runs" can block. Saving a new or changed
// command first asks to confirm the exact command.

const KINDS = {
  webhook: ["Post to a web address", "W", 210], vault_note: ["Add to a vault note", "N", 140],
  channel: ["Send to a channel", "C", 280], command: ["Run a command", ">", 20],
};
const OUTCOME = { ok: ["OK", "ok"], blocked: ["Blocked", "warn"], failed: ["Failed", "error"], timeout: ["Timed out", "error"], skipped: ["Skipped", "muted"] };
const SOURCES = [["any", "Anywhere"], ["chat", "Chats only"], ["task", "Tasks only"], ["agent", "Agents only"]];
const SHELL_TOOLS = "Bash|PowerShell|*run_shell";
const EXAMPLES = [
  { label: "Post agent reports to a web address", event: "agent.inbox", action: "webhook", config: { url: "https://" } },
  { label: "Log every agent result to a vault note", event: "card.review", action: "vault_note", source: "agent",
    config: { path: "Logs/Agent results.md", template: "- {time} {card}: {summary}" } },
  { label: "Tell me on a channel when a card is ready", event: "card.review", action: "channel", config: { template: "Ready for review: {card}" } },
  { label: "Block shell commands that delete files", event: "tool.before", action: "command", tool_pattern: SHELL_TOOLS,
    config: { command: "powershell -NoProfile -Command \"$d = [Console]::In.ReadToEnd(); if ($d -match 'Remove-Item|rm -r|rmdir|del ') { [Console]::Error.WriteLine('Deleting files is blocked by a hook.'); exit 2 }\"" } },
];
const when = (s) => s ? new Date(s * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "never";
const select = (pairs, value) =>
  customSelect({}, pairs.map(([v, t]) => el("option", { value: v, text: t, ...(v === value ? { selected: "" } : {}) })));
const send = (path, method, payload) => api(path, { method, body: JSON.stringify(payload) });

export async function renderHooksPanel(body, _status, page) {
  const [agents, channels] = await Promise.all([api("/api/agents").catch(() => []), api("/api/channels").catch(() => [])]);
  const agentName = new Map(agents.map((a) => [a.id, a.name]));
  const switchFor = (h, label) => toggle({ checked: h.enabled, label, onChange: (on) => send(`/api/hooks/${h.id}`, "PATCH", { enabled: on }) });

  async function showList() {
    const data = await api("/api/hooks");
    if (!page.current) return;
    page.actions([
      el("button", { class: "btn", text: data.paused ? "Resume all" : "Pause all", onclick: async () => {
        await send("/api/hooks/pause", "POST", { paused: !data.paused });
        toast(data.paused ? "Hooks are running again" : "All hooks paused", "success");
        showList();
      } }),
      el("button", { class: "btn primary", text: "Add a hook", onclick: () => showEdit(null, data.events) }),
    ]);
    body.replaceChildren(
      data.paused ? group({}, [row({ title: "All hooks are paused", description: "Nothing below runs until you resume them.", control: pill("Paused", "warn") })]) : "",
      group({ cls: "hook-list" }, data.hooks.length ? data.hooks.map((h) => {
        const [label, letter, hue] = KINDS[h.action];
        const last = (h.log || [])[0];
        const [outcome, tone] = last ? OUTCOME[last.outcome] || [last.outcome, "muted"] : ["Not run yet", "muted"];
        return row({ icon: badge(letter, hue), title: h.name, cls: "hook-row", attrs: { "data-hook": h.id },
          description: `${data.events[h.event]} · ${label.toLowerCase()} · last run ${when(h.last_run_at)}`,
          control: [pill(outcome, tone), switchFor(h, `${h.name} on`), el("span", { class: "set-chevron", text: "›" })] }, () => showHook(h.id));
      }) : [empty("No hooks yet. Add one to run a step of your own when something happens.")]),
      note("Before-tool hooks see every tool Claude and your local and API models use. Codex runs its tools inside its own program, out of JARVIS's sight, so they are not covered. Hooks never set off other hooks, and a failing hook never stops the work it watches unless you set it to."),
    );
  }

  async function showHook(id) {
    const data = await api("/api/hooks");
    const h = data.hooks.find((x) => x.id === id);
    if (!page.current) return;
    if (!h) return showList();
    page.sub({
      title: h.name, description: `${data.events[h.event]}: ${KINDS[h.action][0].toLowerCase()}.`, back: showList,
      actions: [
        el("button", { class: "btn", text: "Send a test event", onclick: async () => {
          const result = await api(`/api/hooks/${id}/test`, { method: "POST" });
          const bad = result.outcome === "failed" || result.outcome === "timeout";
          toast(result.blocked ? `It would block: ${result.blocked}`
            : `Test ${(OUTCOME[result.outcome] || [result.outcome || "done"])[0].toLowerCase()}${result.detail ? `: ${result.detail}` : ""}`, bad ? "error" : "success");
          showHook(id);
        } }),
        el("button", { class: "btn", text: "Edit", onclick: () => showEdit(h, data.events) }),
        el("button", { class: "btn quiet danger", text: "Delete", onclick: async () => {
          if (!await confirmDialog({ title: `Delete ${h.name}?`, message: "It stops running at once.", confirmLabel: "Delete hook" })) return;
          await api(`/api/hooks/${id}`, { method: "DELETE" });
          toast("Hook deleted", "success");
          showList();
        } }),
      ],
    });
    const c = h.config || {};
    const mono = (text) => el("span", { class: "set-mono", text });
    const does = {
      webhook: () => [row({ title: "Web address", description: mono(c.url) }),
        row({ title: "Signed", description: c.secret ? "Each post carries X-JARVIS-Signature: sha256=<HMAC of the body>." : "No secret, so posts are not signed." })],
      vault_note: () => [row({ title: "Note", description: mono(c.path) }), row({ title: "Line", description: mono(c.template) })],
      channel: () => [row({ title: "Channel", description: (channels.find((x) => x.id === c.channel_id) || {}).label || c.channel_id }),
        row({ title: "Message", description: mono(c.template) })],
      command: () => [row({ title: "Command", description: mono(c.command) }),
        row({ title: "Stops after", description: `${c.timeout} seconds` }),
        h.event === "tool.before" ? row({ title: "If it fails", description: c.block_on_failure ? "The tool is blocked." : "The tool goes ahead." }) : null],
    }[h.action]();
    const where = [h.agent_id ? `Only ${agentName.get(h.agent_id) || "a deleted agent"}` : null,
      h.source && h.source !== "any" ? SOURCES.find(([v]) => v === h.source)[1] : null].filter(Boolean).join(" · ") || "Anywhere";
    body.replaceChildren(
      group({ title: "When" }, [
        row({ title: "On", control: switchFor(h, "On") }),
        row({ title: "Event", description: data.events[h.event] }),
        h.tool_pattern && h.event.startsWith("tool.") ? row({ title: "Tools", description: mono(h.tool_pattern) }) : null,
        row({ title: "Where", description: where }),
      ]),
      group({ title: "Does" }, does),
      group({ title: "Recent runs", cls: "hook-runs" }, (h.log || []).length ? h.log.map((entry) => {
        const [label, tone] = OUTCOME[entry.outcome] || [entry.outcome, "muted"];
        return row({ title: data.events[entry.event] || entry.event, description: [when(entry.at), entry.detail].filter(Boolean).join(" · "),
          control: pill(label, tone), cls: "hook-run" });
      }) : [empty("Not run yet. Send a test event to try it.")]),
    );
  }

  function showEdit(existing, events) {
    const start = existing || {};
    const c = start.config || {};
    page.sub({ title: existing ? `Edit ${existing.name}` : "Add a hook", description: "A step of your own that runs when something happens in JARVIS.",
      back: existing ? () => showHook(existing.id) : showList });
    const name = el("input", { value: start.name || "", placeholder: "Log agent results" });
    const event = select(Object.entries(events), start.event || "card.review");
    const action = select(Object.entries(KINDS).map(([k, [label]]) => [k, label]), start.action || "channel");
    const toolPattern = el("input", { value: start.tool_pattern || "", placeholder: SHELL_TOOLS, class: "set-mono" });
    const agent = select([["", "Any agent, or none"], ...agents.map((a) => [a.id, a.name])], start.agent_id || "");
    const source = select(SOURCES, start.source || "any");
    const url = el("input", { value: c.url || "", placeholder: "https://example.com/jarvis" });
    const secret = el("input", { type: "password", autocomplete: "off", placeholder: c.secret ? "Saved. Leave blank to keep it" : "Optional" });
    const notePath = el("input", { value: c.path || "", placeholder: "Logs/Agent results.md" });
    const channel = select([["", "Pick a channel"], ...channels.map((x) => [x.id, x.label])], c.channel_id || "");
    const template = el("input", { value: c.template || "", placeholder: "- {time} {event}: {summary}" });
    const command = el("textarea", { rows: "3", class: "set-mono", placeholder: 'python "C:\\scripts\\guard.py"' });
    command.value = c.command || "";
    const timeout = el("input", { type: "number", value: String(c.timeout || 30), min: "1", max: "120" });
    const blockOnFailure = toggle({ checked: !!c.block_on_failure, label: "Block if it fails" });

    const rows = {
      tool: field("Tools", toolPattern, "Which tools, by name. Separate names with |, and * matches anything. Empty means every tool."),
      webhook: [field("Web address", url), field("Secret", secret, "Signs each post (X-JARVIS-Signature) so the receiver knows it came from JARVIS.")],
      vault_note: [field("Note", notePath, "A path inside the vault. The note is created if it does not exist.")],
      channel: [field("Channel", channel)],
      template: field("Text", template, "Fill in with {field}: {time}, {event}, {summary}, and the event's own fields such as {agent}, {card}, {task}, {reply} or {tool}."),
      command: [row({ stack: true, title: "Command", control: command,
        description: "Runs on this computer with the event as JSON on its input. Exit code 2, or {\"decision\": \"block\", \"reason\": \"...\"} on its output, blocks a tool. Nothing from the event is ever added to this line." }),
      field("Stops after", timeout, "Seconds, up to 120.")],
      blockOnFailure: row({ title: "Block if it fails", description: "If the command fails or times out, block the tool instead of letting it go ahead.", control: blockOnFailure }),
    };
    const sync = () => {
      const kind = action.value, isTool = event.value.startsWith("tool.");
      rows.tool.hidden = !isTool;
      for (const k of Object.keys(KINDS)) for (const r of rows[k]) r.hidden = kind !== k;
      rows.template.hidden = !(kind === "vault_note" || kind === "channel");
      rows.blockOnFailure.hidden = !(kind === "command" && event.value === "tool.before");
    };
    event.addEventListener("change", sync);
    action.addEventListener("change", sync);

    let exampleRow = null;
    if (!existing) {
      const example = select([["", "A blank hook"], ...EXAMPLES.map((x, i) => [String(i), x.label])], "");
      example.addEventListener("change", () => {
        const ex = EXAMPLES[Number(example.value)];
        if (!ex) return;
        name.value = ex.label; event.value = ex.event; action.value = ex.action; source.value = ex.source || "any";
        toolPattern.value = ex.tool_pattern || ""; url.value = ex.config.url || ""; notePath.value = ex.config.path || "";
        template.value = ex.config.template || ""; command.value = ex.config.command || "";
        sync();
      });
      exampleRow = field("Start from", example, "Fills in the form. Change anything before you save.");
    }

    const err = el("div", { class: "set-error" });
    const save = el("button", { class: "btn primary", text: existing ? "Save" : "Add hook", onclick: async () => {
      err.textContent = "";
      const kind = action.value, isTool = event.value.startsWith("tool.");
      const config = kind === "webhook" ? { url: url.value.trim(), ...(secret.value ? { secret: secret.value } : {}) }
        : kind === "vault_note" ? { path: notePath.value.trim(), template: template.value }
        : kind === "channel" ? { channel_id: channel.value, template: template.value }
        : { command: command.value.trim(), timeout: Number(timeout.value) || 30, block_on_failure: event.value === "tool.before" && blockOnFailure.checked };
      if (kind === "command" && config.command && config.command !== (start.action === "command" ? c.command : "")) {
        const ok = await confirmDialog({ title: "Run this command on this computer?", confirmLabel: "Yes, use this command", danger: false,
          message: `${config.command}\n\nIt runs by itself every time this happens: ${events[event.value].toLowerCase()}${isTool ? ", for the tools you picked" : ""}. The event goes to it as JSON on its input.` });
        if (!ok) return;
      }
      const payload = { name: name.value.trim(), event: event.value, action: kind, tool_pattern: isTool ? toolPattern.value.trim() : "",
        agent_id: agent.value || null, source: source.value, config };
      try {
        const saved = await send(existing ? `/api/hooks/${existing.id}` : "/api/hooks", existing ? "PATCH" : "POST", payload);
        toast(existing ? "Saved" : "Hook added", "success");
        showHook(saved.id);
      } catch (problem) { err.textContent = problem.message.replace(/^\d+: /, ""); }
    } });
    body.replaceChildren(group({ cls: "hook-form" }, [
      exampleRow, field("Name", name), field("When", event), rows.tool,
      field("Only for", agent), field("From", source), field("Does", action),
      ...rows.webhook, ...rows.vault_note, ...rows.channel, rows.template, ...rows.command, rows.blockOnFailure,
      el("div", { class: "set-row-actions" }, [err, save]),
    ]));
    sync();
  }

  await showList();
}
