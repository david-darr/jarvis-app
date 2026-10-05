import { api, el, customSelect, toast, confirmDialog } from "../api.js";

// Settings > Channels > Other platforms (core/connectors, 2026-10-05): every
// messaging platform besides Discord. The form for each platform is built
// from the fields its connector declares (GET /api/connectors/kinds), so a
// new platform needs no change here.

const STATE = { listening: "Listening", connected: "Ready", starting: "Starting", error: "Problem", off: "Off" };
const POLL_MS = 5000;

export async function renderConnectors(host, models) {
  const kinds = await api("/api/connectors/kinds");
  const byKind = new Map(kinds.map((k) => [k.kind, k]));
  const list = el("div", { class: "connector-list" });
  host.append(
    el("div", { class: "title", style: "margin-top:22px;", text: "Other platforms" }),
    el("div", { class: "meta", style: "margin:4px 0 12px;", text:
      "Telegram, Slack, Signal, iMessage, email, SMS, WhatsApp and more. Two-way platforms answer only the senders "
      + "you allow; those senders can also reach your agents by name. Send-only ones take task results and agent notifications." }),
    list,
    addForm(kinds, models, () => draw({ force: true })),
  );

  let timer = null;
  async function draw({ force = false } = {}) {
    clearTimeout(timer);
    if (!document.body.contains(list)) return;
    // Someone editing a connector's settings: refreshing would wipe the form.
    if (!force && list.querySelector(".connector-edit[open]")) {
      timer = setTimeout(draw, POLL_MS);
      return;
    }
    const connectors = await api("/api/connectors").catch(() => []);
    list.replaceChildren(...connectors.map((c) => connectorCard(c, byKind.get(c.kind), models, () => draw({ force: true }))));
    if (!connectors.length) list.append(el("div", { class: "meta", text: "None added yet." }));
    timer = setTimeout(draw, POLL_MS);
  }
  await draw();
}

function modelSelect(models, selected) {
  return customSelect({}, [
    el("option", { value: "", text: "No model chosen", ...(selected ? {} : { selected: "" }) }),
    ...models.map((m) => el("option", { value: m.id, text: m.name, ...(m.id === selected ? { selected: "" } : {}) })),
  ]);
}

function fieldInput(field, value, isSet) {
  if (field.kind === "bool") {
    const box = el("input", { type: "checkbox" });
    box.checked = String(value ?? field.default).toLowerCase() === "true";
    return box;
  }
  const input = el(field.kind === "textarea" ? "textarea" : "input", {
    type: field.secret ? "password" : field.kind === "number" ? "number" : "text",
    placeholder: field.secret && isSet ? "Saved; leave blank to keep" : field.placeholder || field.default || "",
    autocomplete: "off",
  });
  if (!field.secret) input.value = value ?? "";
  return input;
}

function fieldsForm(kind, record) {
  const inputs = {};
  const rows = kind.fields.map((field) => {
    const input = fieldInput(field, record?.settings?.[field.key], record?.secrets_set?.[field.key]);
    inputs[field.key] = input;
    return el("div", { class: "field field-grow connector-field" }, [
      el("label", { text: field.label + (field.required ? "" : " (optional)") }), input,
      field.help ? el("div", { class: "meta", text: field.help }) : el("span"),
    ]);
  });
  const values = () => Object.fromEntries(Object.entries(inputs)
    .map(([key, input]) => [key, input.type === "checkbox" ? String(input.checked) : input.value.trim()])
    .filter(([key, value]) => value !== "" || !kind.fields.find((f) => f.key === key).secret));
  return { rows, values };
}

function sendersInput(kind, record) {
  const area = el("textarea", { rows: "2", placeholder: kind.sender_help });
  area.value = (record?.allowed_senders || []).join("\n");
  return area;
}

const senders = (area) => area.value.split(/[\n,]+/).map((s) => s.trim()).filter(Boolean);

function addForm(kinds, models, refresh) {
  const form = el("details", { class: "disclosure-panel" });
  const picker = customSelect({}, kinds.map((k) => el("option", { value: k.kind, text: k.label })));
  const body = el("div", { class: "form-grid" });
  const draw = () => {
    const kind = kinds.find((k) => k.kind === picker.value) || kinds[0];
    const name = el("input", { value: kind.label });
    const { rows, values } = fieldsForm(kind, null);
    const allowed = kind.two_way ? sendersInput(kind, null) : null;
    const model = kind.two_way ? modelSelect(models, "") : null;
    const add = async () => {
      try {
        await api("/api/connectors", { method: "POST", body: JSON.stringify({
          kind: kind.kind, name: name.value.trim(), values: values(),
          allowed_senders: allowed ? senders(allowed) : [], model_endpoint_id: model?.value || null }) });
        toast(`${kind.label} added`, "success");
        form.open = false;
        refresh();
      } catch (problem) { toast(problem.message, "error"); }
    };
    body.replaceChildren(
      el("div", { class: "field field-grow", style: "flex-basis:100%;" }, [
        el("div", { class: "meta", text: kind.description }),
        kind.docs_url ? el("a", { href: kind.docs_url, target: "_blank", rel: "noopener", class: "meta", text: "Setup guide ↗" }) : el("span"),
      ]),
      el("div", { class: "field field-grow" }, [el("label", { text: "Name" }), name]),
      ...rows,
      ...(allowed ? [el("div", { class: "field field-grow", style: "flex-basis:100%;" }, [
        el("label", { text: "Allowed senders" }), allowed, el("div", { class: "meta", text: kind.sender_help })]),
        el("div", { class: "field" }, [el("label", { text: "Model for chats" }), model])] : []),
      el("button", { class: "btn primary", text: `Add ${kind.label}`, onclick: add }),
    );
  };
  picker.addEventListener("change", draw);
  draw();
  form.append(el("summary", { text: "+ Add a platform" }), el("div", { class: "field" }, [el("label", { text: "Platform" }), picker]), body);
  return form;
}

function connectorCard(record, kind, models, refresh) {
  const status = record.status || {};
  const chip = el("span", { class: `connector-state connector-${status.state || "off"}`, text: STATE[status.state] || status.state || "Off" });
  const patch = async (changes, message) => {
    try {
      await api(`/api/connectors/${record.id}`, { method: "PATCH", body: JSON.stringify(changes) });
      if (message) toast(message, "success");
      refresh();
    } catch (problem) { toast(problem.message, "error"); }
  };
  const actions = el("div", { class: "card-row", style: "gap:6px;flex-wrap:wrap;justify-content:flex-start;" }, [
    el("button", { class: "btn quiet", text: record.enabled ? "Turn off" : "Turn on",
      onclick: () => patch({ enabled: !record.enabled }) }),
    el("button", { class: "btn quiet", text: "Send test", onclick: async () => {
      try { await api(`/api/connectors/${record.id}/test`, { method: "POST", body: JSON.stringify({}) }); toast("Sent", "success"); }
      catch (problem) { toast(problem.message, "error"); }
    } }),
    el("button", { class: "btn danger", text: "Remove", onclick: async () => {
      if (!await confirmDialog({ title: `Remove ${record.name}?`, message: "Its saved settings and tokens are deleted. Chats it started stay in Chats.", confirmLabel: "Remove" })) return;
      await api(`/api/connectors/${record.id}`, { method: "DELETE" });
      refresh();
    } }),
  ]);
  const edit = el("details", { class: "disclosure-panel connector-edit" });
  if (kind) {
    const { rows, values } = fieldsForm(kind, record);
    const allowed = kind.two_way ? sendersInput(kind, record) : null;
    const open = kind.two_way ? el("input", { type: "checkbox" }) : null;
    if (open) open.checked = !!record.open;
    const model = kind.two_way ? modelSelect(models, record.model_endpoint_id || "") : null;
    edit.append(el("summary", { text: "Settings" }), el("div", { class: "form-grid" }, [
      ...rows,
      ...(allowed ? [
        el("div", { class: "field field-grow", style: "flex-basis:100%;" }, [el("label", { text: "Allowed senders" }), allowed,
          el("div", { class: "meta", text: kind.sender_help })]),
        el("label", { class: "connector-open" }, [open, el("span", { text: " Anyone can message this (never reaches agents)" })]),
        el("div", { class: "field" }, [el("label", { text: "Model for chats" }), model]),
      ] : []),
      el("button", { class: "btn primary", text: "Save", onclick: () => patch({
        values: values(), ...(allowed ? { allowed_senders: senders(allowed), open: open.checked, model_endpoint_id: model.value || null } : {}),
      }, "Saved") }),
    ]));
  }
  const webhookUrl = record.webhook_path ? location.origin + record.webhook_path : "";
  return el("div", { class: "glass card connector-card" }, [
    el("div", { class: "card-row", style: "justify-content:space-between;gap:10px;flex-wrap:wrap;" }, [
      el("div", {}, [el("div", { class: "title", style: "font-size:13px;", text: record.name }),
        el("div", { class: "meta", text: record.label + (record.two_way ? ` · ${(record.allowed_senders || []).length} allowed sender(s)` : " · send only") })]),
      chip,
    ]),
    status.detail ? el("div", { class: "meta connector-detail", text: status.detail }) : el("span"),
    webhookUrl ? el("div", { class: "meta connector-detail", text:
      `Webhook: ${webhookUrl}. The platform must reach this from the internet; it receives nothing until then.` }) : el("span"),
    actions, edit,
  ]);
}
