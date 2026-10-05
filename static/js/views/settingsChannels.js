import { api, el, customSelect, toast, confirmDialog } from "../api.js";
import { group, row, field, toggle, pill, note, empty, badge, hueFor } from "../settingsKit.js";

// Settings > Channels (redesigned 2026-10-05): Discord bots and every other
// messaging connector (core/connectors) in one list, one row each with its
// live state. A row opens that channel's own page; "Add a channel" opens a
// picker of platform tiles instead of an 18-item dropdown. Each platform's
// form is built from the fields its connector declares
// (GET /api/connectors/kinds), so a new platform needs no change here.

const STATE = {
  listening: ["Listening", "ok"], connected: ["Ready", "ok"], starting: ["Starting", "muted"],
  error: ["Problem", "error"], off: ["Off", "muted"],
};
const POLL_MS = 5000;
const DISCORD_MODES = { normal: "Normal (allow list applies)", open: "Open: anyone can talk", silent: "Silent: never replies" };

const errorText = (problem) => (problem?.message || String(problem)).replace(/^\d+: /, "");
const chevron = () => el("span", { class: "set-chevron", "aria-hidden": "true", text: "›" });

function modelPicker(models, selected, emptyLabel) {
  return customSelect({}, [
    el("option", { value: "", text: emptyLabel, ...(selected ? {} : { selected: "" }) }),
    ...models.map((m) => el("option", { value: m.id, ...(m.id === selected ? { selected: "" } : {}),
      text: m.kind === "claude_cli" || m.kind === "codex_cli" ? `${m.name} (${m.model || "CLI default"})` : `${m.name} (${m.model})` })),
  ]);
}

export async function renderChannelsPanel(body, _status, page) {
  const [kinds, models] = await Promise.all([api("/api/connectors/kinds"), api("/api/models").catch(() => [])]);
  const byKind = new Map(kinds.map((k) => [k.kind, k]));
  let timer = null;
  const stop = () => { clearTimeout(timer); timer = null; };

  // -- the list ----------------------------------------------------------------
  async function showList() {
    stop();
    page.actions([el("button", { class: "btn primary", text: "Add a channel", onclick: showPicker })]);
    const listGroup = group({ cls: "channel-list" }, [empty("Loading…")]);
    body.replaceChildren(listGroup,
      note("Two-way channels answer only the senders you allow, and those senders can reach your agents by name. Send-only ones carry task results and agent notifications."));
    const draw = async () => {
      if (!listGroup.isConnected) return stop();
      const [bots, connectors, health] = await Promise.all([
        api("/api/settings/discord-bots").catch(() => []),
        api("/api/connectors").catch(() => []),
        api("/api/system/status").catch(() => ({})),
      ]);
      if (!listGroup.isConnected) return stop();
      const live = new Set(health.discord_connected_bots || []);
      const rows = [
        ...bots.map((bot) => discordRow(bot, live.has(bot.name))),
        ...connectors.map((record) => connectorRow(record)),
      ];
      listGroup.body.replaceChildren(...(rows.length ? rows : [empty("No channels yet. Add one to reach JARVIS from your phone.")]));
      timer = setTimeout(draw, POLL_MS);
    };
    await draw();
  }

  function discordRow(bot, live) {
    const open = () => showDiscord(bot);
    return row({
      icon: badge("D", 235), cls: "channel-row", attrs: { "data-kind": "discord" },
      title: bot.name,
      description: `Discord · ${bot.allowed_user_id ? "answers one allowed user" : "answers anyone in its channels"}`
        + ((bot.channels || []).length ? ` · ${bot.channels.length} channel override(s)` : ""),
      control: [live ? pill("Listening", "ok") : pill("Not connected", "muted"), chevron()],
    }, open);
  }

  function connectorRow(record) {
    const status = record.status || {};
    const [label, tone] = STATE[status.state] || [status.state || "Off", "muted"];
    const enabled = toggle({ checked: record.enabled, label: `${record.name} on`, onChange: async (on) => {
      await api(`/api/connectors/${record.id}`, { method: "PATCH", body: JSON.stringify({ enabled: on }) });
    } });
    enabled.addEventListener("click", (event) => event.stopPropagation());
    return row({
      icon: badge(record.label, hueFor(record.kind)), cls: "channel-row",
      attrs: { "data-kind": record.kind, "data-state": status.state || "off" },
      title: record.name,
      description: [`${record.label} · ${record.two_way ? `${(record.allowed_senders || []).length} allowed sender(s)` : "send only"}`,
        status.state === "error" && status.detail ? el("div", { class: "channel-problem", text: status.detail }) : null],
      control: [pill(label, tone), enabled, chevron()],
    }, () => showConnector(record));
  }

  // -- the platform picker ---------------------------------------------------
  function showPicker() {
    stop();
    page.sub({ title: "Add a channel", description: "Pick where you want to reach JARVIS, or where results should go.", back: showList });
    const tile = (kind, label, description, onclick) => el("button", { type: "button", class: "set-tile", "data-kind": kind, onclick }, [
      badge(label, kind === "discord" ? 235 : hueFor(kind)),
      el("span", {}, [el("strong", { text: label }), el("span", { class: "meta", text: description })]),
    ]);
    const kindTiles = (list) => list.map((k) => tile(k.kind, k.label, k.description, () => showAdd(k)));
    // Tiles sit straight under their heading (no box around boxes), and a
    // section with nothing in it is left out.
    const section = (title, description, tiles) => tiles.length
      ? el("section", { class: "set-section" }, [el("div", { class: "set-section-head" }, [el("div", {}, [
        el("h3", { class: "set-section-title", text: title }), el("p", { class: "set-section-description", text: description })])]),
        el("div", { class: "set-tiles" }, tiles)]) : null;
    const twoWay = kinds.filter((k) => k.two_way && !k.webhook);
    const viaWebhook = kinds.filter((k) => k.two_way && k.webhook);
    const sendOnly = kinds.filter((k) => !k.two_way);
    body.replaceChildren(...[
      section("Two-way", "Talk to JARVIS and your agents. No public address needed.",
        [tile("discord", "Discord", "A Discord bot in your server or DMs.", showAddDiscord), ...kindTiles(twoWay)]),
      section("Two-way through a webhook", "The platform calls JARVIS, so JARVIS must be reachable from the internet to receive. Sending works regardless.",
        kindTiles(viaWebhook)),
      section("Send only", "For task results and agent notifications. No bot needed.", kindTiles(sendOnly)),
    ].filter(Boolean));
  }

  // -- one connector -----------------------------------------------------------
  function fieldInput(spec, value, isSet) {
    if (spec.kind === "bool") return toggle({ checked: String(value ?? spec.default).toLowerCase() === "true", label: spec.label });
    const input = el(spec.kind === "textarea" ? "textarea" : "input", {
      type: spec.secret ? "password" : spec.kind === "number" ? "number" : "text",
      placeholder: spec.secret && isSet ? "Saved; leave blank to keep" : spec.placeholder || spec.default || "",
      autocomplete: "off",
    });
    if (!spec.secret) input.value = value ?? "";
    return input;
  }

  function fieldRows(kind, record) {
    const inputs = {};
    const rows = kind.fields.map((spec) => {
      const input = fieldInput(spec, record?.settings?.[spec.key], record?.secrets_set?.[spec.key]);
      inputs[spec.key] = input;
      return row({ title: spec.label + (spec.required ? "" : " (optional)"), description: spec.help || "", control: input,
        cls: "set-field connector-field", attrs: { "data-field": spec.key } });
    });
    // Secret fields left blank keep their saved value, so they are not sent.
    const values = () => Object.fromEntries(Object.entries(inputs)
      .map(([key, input]) => [key, input.getAttribute("role") === "switch" ? String(input.checked) : input.value.trim()])
      .filter(([key, value]) => value !== "" || !kind.fields.find((f) => f.key === key).secret));
    return { rows, values };
  }

  const sendersArea = (kind, record) => {
    const area = el("textarea", { rows: "2", placeholder: kind.sender_help });
    area.value = (record?.allowed_senders || []).join("\n");
    return area;
  };
  const senders = (area) => area.value.split(/[\n,]+/).map((s) => s.trim()).filter(Boolean);

  function showConnector(record) {
    stop();
    const kind = byKind.get(record.kind);
    const status = record.status || {};
    const [label, tone] = STATE[status.state] || [status.state || "Off", "muted"];
    const save = async (changes, message) => {
      try {
        const updated = await api(`/api/connectors/${record.id}`, { method: "PATCH", body: JSON.stringify(changes) });
        if (message) toast(message, "success");
        return updated;
      } catch (problem) { toast(errorText(problem), "error"); throw problem; }
    };
    page.sub({
      title: record.name, description: kind ? kind.description : record.label, back: showList,
      actions: [
        el("button", { class: "btn", text: "Send test", onclick: async () => {
          try { await api(`/api/connectors/${record.id}/test`, { method: "POST", body: JSON.stringify({}) }); toast("Sent", "success"); }
          catch (problem) { toast(errorText(problem), "error"); }
        } }),
        el("button", { class: "btn quiet danger", text: "Remove", onclick: async () => {
          if (!await confirmDialog({ title: `Remove ${record.name}?`, message: "Its saved settings and tokens are deleted. Chats it started stay in Chats.", confirmLabel: "Remove" })) return;
          await api(`/api/connectors/${record.id}`, { method: "DELETE" });
          toast(`${record.name} removed`, "success");
          showList();
        } }),
      ],
    });
    const webhookUrl = record.webhook_path ? location.origin + record.webhook_path : "";
    const statusRows = [
      row({ title: "On", description: record.enabled ? "Running now." : "Switched off; nothing is received or sent.",
        control: toggle({ checked: record.enabled, label: "On", onChange: (on) => save({ enabled: on }) }) }),
      row({ title: "Status", description: status.detail || "", control: pill(label, tone), cls: "connector-status" }),
    ];
    if (webhookUrl) {
      statusRows.push(row({ title: "Webhook address", description: [el("div", { class: "set-mono", text: webhookUrl }),
        el("div", { text: "Give this to the platform. It must reach JARVIS from the internet; nothing arrives until then." })],
        control: el("button", { class: "btn", text: "Copy", onclick: async () => { await navigator.clipboard.writeText(webhookUrl); toast("Copied", "success"); } }) }));
    }
    const parts = [group({ title: "Status" }, statusRows)];
    if (kind) {
      const { rows, values } = fieldRows(kind, record);
      const extra = [];
      let allowed = null, open = null, model = null;
      if (kind.two_way) {
        allowed = sendersArea(kind, record);
        open = toggle({ checked: !!record.open, label: "Anyone can message" });
        model = modelPicker(models, record.model_endpoint_id || "", "No model chosen");
        extra.push(
          row({ stack: true, title: "Allowed senders", description: kind.sender_help, control: allowed }),
          row({ title: "Anyone can message", description: "Answers strangers too. They never reach your agents.", control: open }),
          row({ title: "Model for chats", description: "Which model answers messages here.", control: model }),
        );
      }
      const saveBtn = el("button", { class: "btn primary", text: "Save", onclick: async () => {
        const changes = { values: values() };
        if (kind.two_way) Object.assign(changes, { allowed_senders: senders(allowed), open: open.checked, model_endpoint_id: model.value || null });
        try { Object.assign(record, await save(changes, "Saved")); } catch { /* already shown */ }
      } });
      parts.push(group({ title: "Settings", cls: "connector-settings" }, [...rows, ...extra, el("div", { class: "set-row-actions" }, [saveBtn])]));
      if (kind.docs_url) parts.push(note([el("a", { href: kind.docs_url, target: "_blank", rel: "noopener", class: "set-link", text: `${kind.label} setup guide ↗` })]));
    }
    body.replaceChildren(...parts);
  }

  function showAdd(kind) {
    stop();
    page.sub({ title: `Add ${kind.label}`, description: kind.description, back: showPicker });
    const name = el("input", { value: kind.label });
    const { rows, values } = fieldRows(kind, null);
    const allowed = kind.two_way ? sendersArea(kind, null) : null;
    const model = kind.two_way ? modelPicker(models, "", "No model chosen") : null;
    const err = el("div", { class: "set-error" });
    const add = el("button", { class: "btn primary", text: `Add ${kind.label}` });
    add.addEventListener("click", async () => {
      err.textContent = "";
      add.disabled = true;
      try {
        await api("/api/connectors", { method: "POST", body: JSON.stringify({
          kind: kind.kind, name: name.value.trim(), values: values(),
          allowed_senders: allowed ? senders(allowed) : [], model_endpoint_id: model?.value || null }) });
        toast(`${kind.label} added`, "success");
        showList();
      } catch (problem) { err.textContent = errorText(problem); add.disabled = false; }
    });
    body.replaceChildren(
      group({ title: "Settings", cls: "connector-add" }, [
        field("Name", name, "How it appears in JARVIS."), ...rows,
        ...(kind.two_way ? [
          row({ stack: true, title: "Allowed senders", description: kind.sender_help, control: allowed }),
          row({ title: "Model for chats", description: "Which model answers messages here.", control: model }),
        ] : []),
        el("div", { class: "set-row-actions" }, [err, add]),
      ]),
      kind.docs_url ? note([el("a", { href: kind.docs_url, target: "_blank", rel: "noopener", class: "set-link", text: `${kind.label} setup guide ↗` })]) : null,
    );
  }

  // -- Discord ----------------------------------------------------------------
  function showDiscord(bot) {
    stop();
    const reload = async () => {
      const fresh = (await api("/api/settings/discord-bots")).find((b) => b.id === bot.id);
      if (fresh) showDiscord(fresh); else showList();
    };
    page.sub({
      title: bot.name, description: "A Discord bot. Changes here restart its connection at once.", back: showList,
      actions: [el("button", { class: "btn quiet danger", text: "Remove", onclick: async () => {
        const ok = await confirmDialog({ title: "Remove this bot?",
          message: `"${bot.name}" will be disconnected from Discord and its token deleted. Any task delivering to Discord will stop reaching you.`,
          confirmLabel: "Remove bot" });
        if (!ok) return;
        await api(`/api/settings/discord-bots/${bot.id}`, { method: "DELETE" });
        toast("Bot removed", "success");
        showList();
      } })],
    });
    const model = modelPicker(models, bot.model_endpoint_id, "No default model");
    const saveModel = el("button", { class: "btn primary", text: "Save", onclick: async () => {
      await api(`/api/settings/discord-bots/${bot.id}`, { method: "PATCH",
        body: JSON.stringify({ name: bot.name, allowed_user_id: bot.allowed_user_id, model_endpoint_id: model.value || null }) });
      toast("Saved", "success");
      await reload();
    } });

    // Named channels (David's ask 2026-09-10): "open" answers anyone there
    // regardless of the allow list, "silent" never answers there, and either
    // way the channel becomes its own delivery target on the Tasks tab.
    const overrideRows = (bot.channels || []).map((entry) => {
      const mode = customSelect({}, Object.entries(DISCORD_MODES).map(([v, t]) => el("option", { value: v, ...(v === entry.mode ? { selected: "" } : {}) }, t)));
      return row({ title: `#${entry.label}`, description: `Channel ID ${entry.discord_channel_id}`, control: [mode,
        el("button", { class: "btn", text: "Save", onclick: async () => {
          await api(`/api/settings/discord-bots/${bot.id}/channels/${entry.id}`, { method: "PATCH", body: JSON.stringify({ mode: mode.value }) });
          toast("Channel updated", "success");
          await reload();
        } }),
        el("button", { class: "btn quiet danger", text: "Remove", onclick: async () => {
          const ok = await confirmDialog({ title: "Remove this channel override?",
            message: `"#${entry.label}" goes back to normal (allow-list) behaviour and stops being a task delivery target.`, confirmLabel: "Remove" });
          if (!ok) return;
          await api(`/api/settings/discord-bots/${bot.id}/channels/${entry.id}`, { method: "DELETE" });
          toast("Channel removed", "success");
          await reload();
        } })] });
    });
    const chanId = el("input", { placeholder: "Discord channel ID" });
    const chanLabel = el("input", { placeholder: "announcements" });
    const chanMode = customSelect({}, Object.entries(DISCORD_MODES).map(([v, t]) => el("option", { value: v }, t)));
    const chanErr = el("div", { class: "set-error" });
    const addChan = el("button", { class: "btn", text: "Add override", onclick: async () => {
      chanErr.textContent = "";
      if (!chanId.value.trim()) { chanErr.textContent = "A channel ID is required."; return; }
      try {
        await api(`/api/settings/discord-bots/${bot.id}/channels`, { method: "POST", body: JSON.stringify({
          discord_channel_id: chanId.value.trim(), label: chanLabel.value.trim(), mode: chanMode.value || "normal" }) });
        toast("Channel added", "success");
        await reload();
      } catch (problem) { chanErr.textContent = errorText(problem); }
    } });

    body.replaceChildren(
      group({ title: "Bot" }, [
        row({ title: "Allowed user", description: bot.allowed_user_id
          ? `Only Discord user ${bot.allowed_user_id} is answered outside open channels, and only they can reach your agents.`
          : "Answers anyone in its channels. Agents are never reachable through it." }),
        row({ title: "Default model", description: "Which model answers messages to this bot.", control: [model, saveModel] }),
      ]),
      group({ title: "Channel overrides", description: "Per-channel behaviour, and extra delivery targets for tasks." },
        [...(overrideRows.length ? overrideRows : [empty("No overrides. The bot follows its allow list everywhere.")])]),
      group({ title: "Add an override", description: "In Discord, turn on Developer Mode (Settings, Advanced), then right-click a channel and choose Copy Channel ID." }, [
        field("Channel ID", chanId), field("Label", chanLabel), field("Behaviour", chanMode),
        el("div", { class: "set-row-actions" }, [chanErr, addChan]),
      ]),
    );
  }

  function showAddDiscord() {
    stop();
    page.sub({ title: "Add Discord", description: "A Discord bot that answers you in your server or DMs.", back: showPicker });
    const name = el("input", { placeholder: "JARVIS" });
    const token = el("input", { type: "password", placeholder: "Bot token", autocomplete: "off" });
    const allowed = el("input", { placeholder: "Your Discord user ID" });
    const model = modelPicker(models, null, "No default model");
    const err = el("div", { class: "set-error" });
    const add = el("button", { class: "btn primary", text: "Add Discord bot", onclick: async () => {
      err.textContent = "";
      if (!name.value.trim() || !token.value.trim()) { err.textContent = "Name and token are required."; return; }
      try {
        await api("/api/settings/discord-bots", { method: "POST", body: JSON.stringify({
          name: name.value.trim(), token: token.value.trim(), allowed_user_id: allowed.value.trim() || null, model_endpoint_id: model.value || null }) });
        toast("Discord bot added", "success");
        showList();
      } catch (problem) { err.textContent = errorText(problem); }
    } });
    // A bot token isn't something most people have lying around (David's
    // ask 2026-09-01), so the real steps to get one come first.
    const portal = el("a", { href: "https://discord.com/developers/applications", target: "_blank", rel: "noopener", text: "Discord Developer Portal" });
    body.replaceChildren(
      group({ title: "Before you start" }, [el("div", { class: "set-group-pad" }, [el("ol", { class: "set-steps" }, [
        el("li", {}, ["Open the ", portal, " and create a New Application."]),
        el("li", { text: "Open Bot in the sidebar, click Reset Token and copy it." }),
        el("li", { text: "Under Privileged Gateway Intents, turn on Message Content Intent." }),
        el("li", { text: "Under OAuth2, URL Generator, tick \"bot\" with Send Messages and Read Message History, then open the URL to invite it." }),
      ])])]),
      group({ title: "Settings" }, [
        field("Name", name), field("Bot token", token),
        field("Allowed user", allowed, "Optional. Only this user is answered, and only they can reach your agents. Leave empty to answer anyone in its channels."),
        field("Default model", model),
        el("div", { class: "set-row-actions" }, [err, add]),
      ]),
      note("The plain \"Discord\" delivery target messages the allowed user directly; each channel override is its own target on the Tasks tab."),
    );
  }

  await showList();
}
