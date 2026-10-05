import { api, el, customSelect, toast, confirmDialog } from "../api.js";
import { group, row, field, toggle, pill, note, empty, badge, hueFor } from "../settingsKit.js";

// Tasks > Triggers (2026-10-05; services/trigger_service.py, spec: the vault
// note "Webhook Triggers (Build Spec)"). A trigger is a web address an
// outside service (GitHub, a form, a script) sends signed events to; each one
// becomes a card or a run of a task. By default the work waits for the
// person's OK, here or by replying to the notification; "Run straight away"
// is a per-trigger switch he turns on himself.

const OUTCOME = {
  waiting: ["Waiting for you", "warn"], started: ["Started", "ok"], approved: ["Approved", "ok"], declined: ["Skipped", "muted"],
  filtered: ["Filtered out", "muted"], duplicate: ["Duplicate", "muted"], ping: ["Setup check", "muted"],
  rejected: ["Rejected", "error"], rate_limited: ["Too many", "error"], failed: ["Failed", "error"],
};
const when = (seconds) => seconds ? new Date(seconds * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "never";

export async function renderTriggers(host) {
  const [agents, tasks, channels] = await Promise.all([
    api("/api/agents").catch(() => []), api("/api/tasks").catch(() => []), api("/api/channels").catch(() => []),
  ]);
  const agentName = new Map(agents.map((a) => [a.id, a.name]));
  const runnable = tasks.filter((t) => t.schedule_kind !== "card");
  const taskName = new Map(runnable.map((t) => [t.id, t.name]));

  const doesWhat = (t) => t.action === "task"
    ? `runs "${taskName.get(t.task_id) || "a removed task"}"`
    : `makes a card for ${t.agent_id ? agentName.get(t.agent_id) || "a deleted agent" : "JARVIS"}`;

  async function showList() {
    const data = await api("/api/triggers");
    const parts = [
      el("div", { class: "card-row triggers-head" }, [
        el("div", {}, [
          el("div", { class: "title", text: "Triggers" }),
          el("div", { class: "meta", text: "Outside events, like a GitHub push or a form, that start work. Each waits for your OK unless you switch on Run straight away." }),
        ]),
        el("button", { class: "btn", text: "Add a trigger", onclick: showAdd }),
      ]),
    ];
    if (data.pending.length) {
      parts.push(group({ title: "Waiting for you" }, data.pending.map((p) => {
        const answer = async (approve) => {
          try {
            const res = await api(`/api/triggers/pending/${p.id}`, { method: "POST", body: JSON.stringify({ approve }) });
            toast(res.next, "success");
          } catch (problem) { toast(problem.message, "error"); }
          showList();
        };
        return row({ title: `${p.trigger_name}: ${p.summary}`, description: `${p.event_type} · ${when(p.created_at)}`, cls: "trigger-pending",
          control: [el("button", { class: "btn quiet", text: "Skip", onclick: () => answer(false) }),
            el("button", { class: "btn primary", text: "Approve", onclick: () => answer(true) })] });
      })));
    }
    parts.push(group({ cls: "trigger-list" }, data.triggers.length ? data.triggers.map((t) => {
      const on = toggle({ checked: t.enabled, label: `${t.name} on`, onChange: (value) =>
        api(`/api/triggers/${t.id}`, { method: "PATCH", body: JSON.stringify({ enabled: value }) }) });
      on.addEventListener("click", (event) => event.stopPropagation());
      return row({ icon: badge(t.preset === "github" ? "G" : "W", t.preset === "github" ? 260 : hueFor(t.name)),
        title: t.name, cls: "trigger-row", attrs: { "data-trigger": t.id },
        description: `${t.preset === "github" ? "GitHub" : "Webhook"} · ${doesWhat(t)} · last event ${when(t.last_event_at)}`,
        control: [t.auto_run ? pill("Runs straight away", "accent") : pill("Asks first", "muted"), on, el("span", { class: "set-chevron", text: "›" })],
      }, () => showTrigger(t.id));
    }) : [empty("No triggers yet. Add one to let GitHub or a form start work.")]));
    host.replaceChildren(...parts);
  }

  function secretBox(trigger, secret) {
    const url = location.origin + trigger.path;
    return group({ title: "Connect it", cls: "trigger-secret" }, [
      row({ title: "Address", description: el("span", { class: "set-mono", text: url }),
        control: el("button", { class: "btn", text: "Copy", onclick: () => navigator.clipboard.writeText(url).then(() => toast("Copied", "success")) }) }),
      row({ title: "Secret", description: [el("span", { class: "set-mono", text: secret }),
        el("div", { text: "Shown this once. Make a new one any time; the old one then stops working." })],
        control: el("button", { class: "btn", text: "Copy", onclick: () => navigator.clipboard.writeText(secret).then(() => toast("Copied", "success")) }) }),
      row({ title: trigger.preset === "github" ? "In GitHub" : "From your service", description: trigger.preset === "github"
        ? "Repository Settings, Webhooks, Add webhook: paste the address as the Payload URL, choose application/json, paste the secret, pick the events."
        : "POST JSON to the address with the header X-JARVIS-Signature: sha256=<HMAC-SHA256 of the body with the secret>. Optional: X-JARVIS-Delivery with a unique id per event." }),
      note("Events only arrive once JARVIS can be reached from the internet. Until then you can test from this computer."),
    ]);
  }

  // -- one trigger -------------------------------------------------------------
  async function showTrigger(id, freshSecret = null) {
    const data = await api("/api/triggers");
    const t = data.triggers.find((x) => x.id === id);
    if (!t) return showList();
    const save = async (changes, message = "Saved") => {
      try { await api(`/api/triggers/${id}`, { method: "PATCH", body: JSON.stringify(changes) }); toast(message, "success"); }
      catch (problem) { toast(problem.message, "error"); throw problem; }
    };
    const events = el("input", { value: (t.events || []).join(", "), placeholder: "Any event" });
    const condPath = el("input", { value: t.conditions?.[0]?.path || "", placeholder: "ref" });
    const condValue = el("input", { value: t.conditions?.[0]?.equals || "", placeholder: "refs/heads/main" });
    const title = el("input", { value: t.title_template || "", placeholder: "{event_type}" });
    const prompt = el("textarea", { rows: "3" });
    prompt.value = t.prompt_template || "";
    const parts = [
      el("button", { class: "btn quiet set-back", text: "← Triggers", onclick: showList }),
      el("div", { class: "card-row triggers-head" }, [
        el("div", {}, [el("div", { class: "title", text: t.name }), el("div", { class: "meta", text: `${doesWhat(t)}.` })]),
        el("div", { class: "card-row", style: "gap:6px;" }, [
          el("button", { class: "btn", text: "New secret", onclick: async () => {
            const ok = await confirmDialog({ title: "Make a new secret?", message: "The current one stops working at once; update it wherever it is used.", confirmLabel: "Make a new secret", danger: false });
            if (!ok) return;
            const res = await api(`/api/triggers/${id}/secret`, { method: "POST" });
            showTrigger(id, res.secret);
          } }),
          el("button", { class: "btn quiet danger", text: "Delete", onclick: async () => {
            const ok = await confirmDialog({ title: `Delete ${t.name}?`, message: "Its address stops working. Cards it already made stay on the board.", confirmLabel: "Delete trigger" });
            if (!ok) return;
            await api(`/api/triggers/${id}`, { method: "DELETE" });
            toast("Trigger deleted", "success");
            showList();
          } }),
        ]),
      ]),
    ];
    if (freshSecret) parts.push(secretBox(t, freshSecret));
    else parts.push(group({}, [row({ title: "Address", description: el("span", { class: "set-mono", text: location.origin + t.path }),
      control: el("button", { class: "btn", text: "Copy", onclick: () => navigator.clipboard.writeText(location.origin + t.path).then(() => toast("Copied", "success")) }) })]));
    parts.push(group({ title: "Behaviour" }, [
      row({ title: "On", description: "Off makes the address refuse events.", control: toggle({ checked: t.enabled, label: "On", onChange: (v) => save({ enabled: v }) }) }),
      row({ title: "Run straight away", description: "Starts the work without asking you first. Only for sources you trust: the event is treated as information, but the work runs with its agent's tools.",
        control: toggle({ checked: t.auto_run, label: "Run straight away", onChange: (v) => save({ auto_run: v }) }) }),
    ]));
    parts.push(group({ title: "Filters and wording", cls: "trigger-settings" }, [
      field("Events", events, "Comma-separated, such as push, pull_request. Empty takes every event."),
      field("Only when field", condPath, "A field in the event, such as ref. Leave empty for no condition."),
      field("equals", condValue),
      ...(t.action === "card" ? [field("Card title", title, "Fill in from the event with {field.path}, for example {head_commit.message}."),
        row({ stack: true, title: "Instructions", description: "What to do. {field.path} works here too; the whole event is attached as information.", control: prompt })] : []),
      el("div", { class: "set-row-actions" }, [el("button", { class: "btn primary", text: "Save", onclick: () => save({
        events: events.value.split(",").map((s) => s.trim()).filter(Boolean),
        conditions: condPath.value.trim() ? [{ path: condPath.value.trim(), equals: condValue.value }] : [],
        ...(t.action === "card" ? { title_template: title.value, prompt_template: prompt.value } : {}),
      }).then(() => showTrigger(id)).catch(() => {}) })]),
    ]));
    parts.push(group({ title: "Recent events" }, (t.log || []).length ? t.log.map((entry) => {
      const [label, tone] = OUTCOME[entry.outcome] || [entry.outcome, "muted"];
      return row({ title: entry.event || "event", description: [when(entry.at), entry.detail].filter(Boolean).join(" · "), control: pill(label, tone), cls: "trigger-event" });
    }) : [empty("Nothing received yet.")]));
    host.replaceChildren(...parts);
  }

  // -- add -----------------------------------------------------------------------
  function showAdd() {
    const name = el("input", { placeholder: "GitHub pushes" });
    const preset = customSelect({}, [el("option", { value: "github", text: "GitHub" }), el("option", { value: "generic", text: "Any service (signed JSON)" })]);
    const action = customSelect({}, [el("option", { value: "card", text: "Make a card" }), ...(runnable.length ? [el("option", { value: "task", text: "Run a task or agent goal" })] : [])]);
    const agent = customSelect({}, [el("option", { value: "", text: "JARVIS (no agent)" }), ...agents.map((a) => el("option", { value: a.id, text: a.name }))]);
    const task = customSelect({}, runnable.map((t) => el("option", { value: t.id, text: t.agent_id ? `${agentName.get(t.agent_id) || "Agent"}: ${t.name}` : t.name })));
    const events = el("input", { placeholder: "push" });
    const title = el("input", { placeholder: "Push to {repository.full_name}: {head_commit.message}" });
    const prompt = el("textarea", { rows: "3", placeholder: "Review the push to {ref} and tell me if anything looks risky." });
    const channel = customSelect({}, [el("option", { value: "", text: "In the app, or the agent's channel" }),
      ...channels.map((c) => el("option", { value: c.id, text: c.label }))]);
    const agentRow = field("For", agent, "The agent who does the work.");
    const taskRow = field("Task", task);
    const titleRow = field("Card title", title, "Fill in from the event with {field.path}.");
    const promptRow = row({ stack: true, title: "Instructions", description: "What to do. The whole event is attached as information, never as instructions.", control: prompt });
    const sync = () => {
      const isCard = action.value === "card";
      agentRow.hidden = titleRow.hidden = promptRow.hidden = !isCard;
      taskRow.hidden = isCard;
    };
    action.addEventListener("change", sync);
    sync();
    const err = el("div", { class: "set-error" });
    const create = el("button", { class: "btn primary", text: "Add trigger", onclick: async () => {
      err.textContent = "";
      try {
        const made = await api("/api/triggers", { method: "POST", body: JSON.stringify({
          name: name.value.trim(), preset: preset.value, action: action.value,
          agent_id: action.value === "card" ? agent.value || null : null, task_id: action.value === "task" ? task.value : null,
          events: events.value.split(",").map((s) => s.trim()).filter(Boolean),
          title_template: title.value, prompt_template: prompt.value, deliver_to_channel: channel.value || null }) });
        toast("Trigger added", "success");
        showTrigger(made.id, made.secret);
      } catch (problem) { err.textContent = problem.message; }
    } });
    host.replaceChildren(
      el("button", { class: "btn quiet set-back", text: "← Triggers", onclick: showList }),
      el("div", { class: "title", text: "Add a trigger" }),
      group({ cls: "trigger-add" }, [
        field("Name", name), field("Source", preset), field("Does", action), agentRow, taskRow,
        field("Events", events, "Comma-separated. Empty takes every event."), titleRow, promptRow,
        field("Ask me on", channel, "Where the request for your OK goes."),
        el("div", { class: "set-row-actions" }, [err, create]),
      ]),
      note("New triggers ask for your OK before anything runs. You can switch that off per trigger afterwards."),
    );
    name.focus();
  }

  await showList();
}
