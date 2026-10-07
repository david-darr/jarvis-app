import { api, el, toast } from "../api.js";
import { ICONS } from "../icons.js";

// Browse what Kairos can use. Skill content and live MCP tool schemas stay
// behind their own read/describe calls; this view only loads catalog metadata.
export async function render(container) {
  container.innerHTML = "";
  const state = { query: "", kind: "all", installed: false, skills: [], servers: [], integrations: [], skillsError: "", catalogError: "" };
  const search = el("input", { type: "search", class: "tool-store-search", placeholder: "Search skills and tools", "aria-label": "Search skills and tools" });
  const filters = el("div", { class: "segmented-tabs tool-store-filters", role: "group", "aria-label": "Store category" });
  const installed = el("label", { class: "tool-store-installed" }, [
    el("input", { type: "checkbox" }), el("span", { text: "Added only" }),
  ]);
  const results = el("div", { class: "tool-store-results" });
  const count = el("span", { class: "meta", "aria-live": "polite" });
  const manageSkills = el("button", { type: "button", class: "btn quiet", text: "Manage skills" });
  const manageTools = el("button", { type: "button", class: "btn quiet", text: "Add MCP server", disabled: true });
  const remoteInstall = el("details", { class: "disclosure-panel tool-store-install", hidden: true });
  const customServer = el("details", { class: "disclosure-panel tool-store-install", hidden: true });
  const managerHost = el("div", { class: "tool-store-manager", hidden: true });
  manageSkills.addEventListener("click", () => showSkillManager());
  manageTools.addEventListener("click", () => { customServer.open = true; customServer.scrollIntoView({ block: "nearest" }); });

  container.append(el("div", { class: "view-constrained tool-store-view" }, [
    el("div", { class: "view-header" }, [
      el("div", {}, [
        el("h2", { text: "Tool Store" }),
        el("div", { class: "sub", text: "Find skills and connect tools. Choose connected servers in each chat's Integrations menu." }),
      ]),
    ]),
    el("div", { class: "tool-store-toolbar" }, [search, filters, installed]),
    el("div", { class: "tool-store-summary" }, [count, el("div", { class: "tool-store-manage" }, [manageSkills, manageTools])]),
    remoteInstall, customServer, managerHost,
    results,
  ]));
  results.append(el("div", { class: "tool-store-empty", role: "status", text: "Loading skills and tools…" }));

  const sourceInput = el("input", { type: "url", placeholder: "https://github.com/owner/repo/blob/main/path/SKILL.md", "aria-label": "GitHub SKILL.md URL", required: true });
  const installButton = el("button", { type: "button", class: "btn", text: "Install skill" });
  const installStatus = el("div", { class: "meta", role: "status" });
  const installReview = el("div", { class: "tool-store-review" });
  remoteInstall.append(
    el("summary", { text: "Install a single-file skill from GitHub" }),
    el("p", { class: "meta", text: "Paste a public SKILL.md file link. Kairos scans it before adding it; a flagged skill may need your review. Companion files are not imported." }),
    el("div", { class: "tool-store-form-row" }, [sourceInput, installButton]),
    installStatus, installReview,
  );
  installButton.addEventListener("click", () => installSkill(false));

  const serverName = el("input", { placeholder: "Server name", "aria-label": "Server name" });
  const serverUrl = el("input", { type: "url", placeholder: "https://example.com/mcp", "aria-label": "MCP server URL" });
  const serverAuth = el("select", { "aria-label": "Authentication" }, [
    el("option", { value: "none", text: "No sign-in" }),
    el("option", { value: "oauth", text: "OAuth sign-in" }),
    el("option", { value: "key", text: "Bearer token" }),
  ]);
  const serverKey = el("input", { type: "password", placeholder: "Bearer token", "aria-label": "Bearer token", hidden: true });
  const serverButton = el("button", { type: "button", class: "btn", text: "Add server" });
  const serverStatus = el("div", { class: "tool-store-action", role: "status" });
  serverAuth.addEventListener("change", () => { serverKey.hidden = serverAuth.value !== "key"; });
  customServer.append(
    el("summary", { text: "Add a custom MCP server" }),
    el("p", { class: "meta", text: "Connect a third-party HTTP MCP endpoint. Its tools still use Kairos's permission prompts and are selected per chat." }),
    el("div", { class: "tool-store-form-row" }, [serverName, serverUrl, serverAuth, serverKey, serverButton]),
    serverStatus,
    el("button", { type: "button", class: "btn quiet", text: "Advanced connection settings", onclick: () => navigate("settings", { section: "integrations" }) }),
  );
  serverButton.addEventListener("click", addServer);

  search.addEventListener("input", () => { state.query = search.value.trim().toLowerCase(); draw(); });
  installed.querySelector("input").addEventListener("change", (event) => {
    state.installed = event.target.checked;
    draw();
  });
  const loaded = await Promise.allSettled([
    api("/api/skills"), api("/api/integrations/catalog"), api("/api/integrations"),
  ]);
  if (!container.isConnected) return;
  if (loaded[0].status === "fulfilled") state.skills = loaded[0].value;
  else state.skillsError = "Skills are unavailable right now.";
  if (loaded[1].status === "fulfilled") state.servers = loaded[1].value;
  else state.catalogError = loaded[1].reason?.message?.startsWith("403:")
    ? "Only an admin can browse and add tool connections."
    : "The tool catalog is unavailable right now.";
  if (loaded[2].status === "fulfilled") state.integrations = loaded[2].value;
  if (loaded[1].status === "fulfilled") {
    remoteInstall.hidden = false;
    customServer.hidden = false;
    manageTools.disabled = false;
  } else if (state.catalogError.startsWith("Only an admin")) manageTools.remove();
  draw();

  async function showSkillManager(focusSlug = null) {
    if (!focusSlug && !managerHost.hidden) {
      managerHost.hidden = true;
      managerHost.replaceChildren();
      manageSkills.textContent = "Manage skills";
      try { state.skills = await api("/api/skills"); state.skillsError = ""; }
      catch { state.skillsError = "Skills could not refresh. Reopen the store."; }
      draw();
      return;
    }
    managerHost.hidden = false;
    manageSkills.textContent = "Close skill manager";
    const { renderSkillManager } = await import("../skillManager.js");
    if (!managerHost.isConnected) return;
    await renderSkillManager(managerHost, focusSlug);
    managerHost.scrollIntoView({ block: "nearest" });
  }

  async function installSkill(confirmed, expectedSha256 = null) {
    const url = sourceInput.value.trim();
    if (!url) { sourceInput.focus(); return; }
    installButton.disabled = true;
    installStatus.textContent = "Downloading and scanning…";
    installReview.replaceChildren();
    try {
      const response = await fetch("/api/skills/install-url", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url, confirmed, expected_sha256: expectedSha256 }),
      });
      const payload = await response.json().catch(() => ({}));
      if (response.ok) {
        toast(`Installed ${payload.slug}`, "success");
        sourceInput.value = "";
        installStatus.textContent = "Skill installed.";
        try { state.skills = await api("/api/skills"); state.skillsError = ""; }
        catch { state.skillsError = "Skill installed, but the list could not refresh. Reopen the store."; }
        draw();
        return;
      }
      const detail = payload.detail;
      installStatus.textContent = typeof detail === "string" ? detail : "Review the scan below.";
      if (response.status === 409 && detail && typeof detail === "object") {
        const actions = [];
        if (detail.needs_confirmation && detail.sha256) actions.push(
          el("button", { type: "button", class: "btn danger", text: "Install anyway", onclick: () => installSkill(true, detail.sha256) }),
        );
        installReview.append(el("div", { class: "tool-store-scan" }, [
          el("div", { class: "title", text: detail.needs_confirmation ? "Review this skill" : "Installation blocked" }),
          el("pre", { text: detail.report || "" }),
          ...actions,
        ]));
      }
    } catch (error) { installStatus.textContent = `Install failed: ${error.message}`; }
    finally { installButton.disabled = false; }
  }

  async function addServer() {
    const name = serverName.value.trim();
    const url = serverUrl.value.trim();
    if (!name) { serverName.focus(); return; }
    if (!url) { serverUrl.focus(); return; }
    try {
      const parsed = new URL(url);
      if (!["http:", "https:"].includes(parsed.protocol) || parsed.username || parsed.password || parsed.hash) throw new Error("URL");
    } catch { serverStatus.textContent = "Enter an HTTP or HTTPS MCP server URL."; serverUrl.focus(); return; }
    if (state.integrations.some((item) => item.kind === "mcp_server" &&
      (item.url || "").replace(/\/$/, "") === url.replace(/\/$/, ""))) {
      serverStatus.textContent = "This server is already added.";
      return;
    }
    if (serverAuth.value === "key" && !serverKey.value.trim()) { serverKey.focus(); return; }
    serverButton.disabled = true;
    serverStatus.textContent = "Adding server…";
    try {
      const item = await api("/api/integrations/mcp-server", {
        method: "POST", body: JSON.stringify({ name, mcp_type: "http", url,
          ...(serverAuth.value === "oauth" ? { auth: "oauth" } : {}),
          ...(serverAuth.value === "key" ? { api_key: serverKey.value.trim() } : {}) }),
      });
      state.integrations.push(item);
      const catalogMatch = state.servers.find((server) => server.url.replace(/\/$/, "") === item.url.replace(/\/$/, ""));
      if (catalogMatch) catalogMatch.added = true;
      serverName.value = ""; serverUrl.value = ""; serverKey.value = "";
      if (item.auth === "oauth") await signIn(item.id, item.name, serverStatus);
      else { serverStatus.textContent = "Server added."; toast(`${item.name} added`, "success"); draw(); }
    } catch (error) { serverStatus.textContent = error.message; }
    finally { serverButton.disabled = false; }
  }

  function draw() {
    filters.innerHTML = "";
    for (const [id, label] of [["all", "All"], ["skills", "Skills"], ["tools", "Tools"]]) {
      const button = el("button", {
        type: "button", class: "segmented-tab" + (state.kind === id ? " active" : ""),
        text: label, "aria-pressed": state.kind === id ? "true" : "false",
      });
      button.addEventListener("click", () => { state.kind = id; draw(); });
      filters.append(button);
    }
    results.innerHTML = "";
    const q = state.query;
    const skills = state.kind === "tools" ? [] : state.skills.filter((item) =>
      !q || `${item.slug} ${item.description}`.toLowerCase().includes(q));
    const tools = state.kind === "skills" ? [] : state.servers.filter((item) =>
      (!q || `${item.name} ${item.description || ""}`.toLowerCase().includes(q)) &&
      (!state.installed || item.added));
    const custom = state.kind === "skills" ? [] : state.integrations.filter((item) => item.kind === "mcp_server" &&
      !state.servers.some((server) => server.url.replace(/\/$/, "") === (item.url || "").replace(/\/$/, "")) &&
      (!q || `${item.name} ${item.url || ""}`.toLowerCase().includes(q)));
    const total = skills.length + tools.length + custom.length;
    count.textContent = `${total} result${total === 1 ? "" : "s"}`;
    if (skills.length) {
      results.append(el("h3", { class: "tool-store-heading", text: `Skills · ${skills.length}` }));
      const grid = el("div", { class: "tool-store-grid" });
      for (const item of skills) grid.append(skillCard(item));
      results.append(grid);
    }
    if (tools.length || custom.length) {
      results.append(el("h3", { class: "tool-store-heading", text: `Tools · ${tools.length + custom.length}` }));
      const grid = el("div", { class: "tool-store-grid" });
      for (const item of tools) grid.append(toolCard(item));
      for (const item of custom) grid.append(customToolCard(item));
      results.append(grid);
    }
    if (state.kind !== "skills" && state.catalogError) {
      results.append(el("div", { class: "tool-store-notice", text: state.catalogError }));
    }
    if (state.kind !== "tools" && state.skillsError) {
      results.append(el("div", { class: "tool-store-notice", text: state.skillsError }));
    }
    if (!total && !(state.kind !== "skills" && state.catalogError) && !(state.kind !== "tools" && state.skillsError)) {
      results.append(el("div", { class: "tool-store-empty", text: q ? "No matches. Try another search." : "Nothing in this view yet." }));
    }
  }

  // A skill whose SKILL.md can't be read (roadmap phase 6): no model gets it;
  // here it says why and can be deleted.
  function brokenSkillCard(item) {
    return el("article", { class: "tool-store-card tool-store-broken", "data-skill": item.slug }, [
      el("div", { class: "tool-store-card-top" }, [
        el("span", { class: "tool-store-mark" }, [svg(ICONS.brain)]),
        el("span", { class: "tool-store-badge blocked", text: "Can't be read" }),
      ]),
      el("h4", { text: item.slug }),
      el("p", { text: `${item.error} No model is offered it until it is fixed or deleted.` }),
      el("div", { class: "tool-store-card-foot" }, [
        el("span", { class: "meta", text: "Unreadable skill" }),
        el("button", { type: "button", class: "btn danger", text: "Delete", onclick: async () => {
          try {
            await api(`/api/skills/${encodeURIComponent(item.slug)}`, { method: "DELETE" });
            state.skills = state.skills.filter((s) => s.slug !== item.slug);
            toast(`Deleted ${item.slug}`, "success");
            draw();
          } catch (error) { toast(error.message, "error"); }
        } }),
      ]),
    ]);
  }

  // An MCP server's health and the tools waiting for review (roadmap phase 6,
  // core/integrations.py): a check is kept on the server, and a tool that is
  // new or changed since it was pinned is held from every model until accepted.
  function healthEl(integration) {
    const status = integration.status;
    const when = status?.checked_at ? new Date(status.checked_at * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "";
    const text = !status ? "Not checked yet"
      : status.state === "working" ? `Working · ${status.tools} tool${status.tools === 1 ? "" : "s"} · checked ${when}`
      : status.state === "signed_out" ? "Not signed in"
      : `Not responding · ${status.error || "no answer"} · checked ${when}`;
    const check = el("button", { type: "button", class: "btn quiet", text: "Check" });
    check.addEventListener("click", async () => {
      check.disabled = true;
      check.textContent = "Checking…";
      try {
        const updated = await api(`/api/integrations/${integration.id}/check`, { method: "POST" });
        Object.assign(integration, updated);
        draw();
      } catch (error) { toast(error.message, "error"); check.disabled = false; check.textContent = "Check"; }
    });
    const nodes = [el("div", { class: `tool-store-health health-${status?.state || "unknown"}` }, [el("span", { text }), check])];
    const held = integration.held_tools || [];
    if (held.length) {
      const accept = async (names) => {
        try {
          Object.assign(integration, await api(`/api/integrations/${integration.id}/tools/accept`,
            { method: "POST", body: JSON.stringify({ names }) }));
          toast(names.length === 1 ? `${names[0]} accepted` : `${names.length} tools accepted`, "success");
          draw();
        } catch (error) { toast(error.message, "error"); }
      };
      nodes.push(el("div", { class: "tool-store-held" }, [
        el("div", { class: "title", text: `${held.length} tool${held.length === 1 ? "" : "s"} new or changed: held from every model until you accept` }),
        ...held.map((tool) => el("div", { class: "tool-store-held-tool" }, [
          el("div", {}, [
            el("strong", { text: tool.name }),
            el("span", { class: "meta", text: tool.kind === "new" ? " · new" : " · changed since you added it" }),
            el("div", { class: "meta", text: tool.description || "No description." }),
          ]),
          el("button", { type: "button", class: "btn quiet", text: "Accept", onclick: () => accept([tool.name]) }),
        ])),
        ...(held.length > 1 ? [el("button", { type: "button", class: "btn", text: "Accept all", onclick: () => accept(held.map((t) => t.name)) })] : []),
      ]));
    }
    return nodes;
  }

  function skillCard(item) {
    if (item.error) return brokenSkillCard(item);
    const blocked = !!item.curation?.blocked_for_models;
    const detail = el("div", { class: "tool-store-detail" });
    const button = el("button", { type: "button", class: "btn quiet", text: "View skill", "aria-expanded": "false" });
    button.addEventListener("click", async () => {
      if (button.getAttribute("aria-expanded") === "true") {
        detail.replaceChildren(); button.setAttribute("aria-expanded", "false"); return;
      }
      button.disabled = true;
      try {
        const full = await api(`/api/skills/${encodeURIComponent(item.slug)}`);
        if (!button.isConnected) return;
        detail.replaceChildren(
          el("pre", { class: "tool-store-skill-body", text: full.body || "This skill has no body yet." }),
          el("button", { type: "button", class: "btn quiet", text: "Manage skill", onclick: () => showSkillManager(item.slug) }),
        );
        button.setAttribute("aria-expanded", "true");
      } catch (error) { toast(error.message, "error"); }
      finally { button.disabled = false; }
    });
    const source = item.curation?.source;
    const origin = item.curation?.origin || "";
    const sourceLabel = origin.startsWith("https://raw.githubusercontent.com/")
      ? el("a", { href: origin, target: "_blank", rel: "noopener", text: "GitHub source" })
      : el("span", { class: "meta", text: source === "bundled" ? "Built into Kairos" : source === "imported" ? "Imported skill" : "Local skill" });
    return el("article", { class: "tool-store-card" }, [
      el("div", { class: "tool-store-card-top" }, [
        el("span", { class: "tool-store-mark" }, [svg(ICONS.brain)]),
        el("span", { class: "tool-store-badge" + (blocked ? " blocked" : ""), text: blocked ? "Needs approval" : "Available" }),
      ]),
      el("h4", { text: item.slug }),
      el("p", { text: item.description || "No description provided." }),
      el("div", { class: "tool-store-card-foot" }, [
        item.version ? el("span", { class: "tool-store-version" }, [sourceLabel, el("span", { class: "meta", text: ` · v${item.version}` })]) : sourceLabel,
        button,
      ]),
      detail,
    ]);
  }

  function toolCard(server) {
    const integration = state.integrations.find((item) => item.kind === "mcp_server" &&
      (item.url || "").replace(/\/$/, "") === server.url.replace(/\/$/, ""));
    const connected = server.added && (server.auth !== "oauth" || !!integration?.signed_in);
    const actionHost = el("div", { class: "tool-store-action" });
    let action;
    if (connected) action = el("span", { class: "tool-store-badge", text: "Added" });
    else if (integration && server.auth === "oauth") {
      action = el("button", { type: "button", class: "btn", text: "Sign in", onclick: () => signIn(integration.id, server.name, actionHost) });
    } else if (server.added) {
      action = el("button", { type: "button", class: "btn quiet", text: "Manage", onclick: () => navigate("settings", { section: "integrations" }) });
    } else {
      action = el("button", { type: "button", class: "btn", text: server.auth === "oauth" ? "Add and sign in" : "Add" });
      action.addEventListener("click", async () => {
        action.disabled = true;
        try {
          const item = await api("/api/integrations/mcp-server", {
            method: "POST",
            body: JSON.stringify({ name: server.name, mcp_type: "http", url: server.url,
              ...(server.auth === "oauth" ? { auth: "oauth" } : {}) }),
          });
          server.added = true;
          state.integrations.push(item);
          if (server.auth === "oauth") await signIn(item.id, server.name, actionHost);
          else { toast(`${server.name} added`, "success"); draw(); }
        } catch { action.disabled = false; }
      });
    }
    actionHost.append(action);
    return el("article", { class: "tool-store-card" }, [
      el("div", { class: "tool-store-card-top" }, [
        el("span", { class: "tool-store-mark" }, [svg(ICONS.store)]),
        integration?.status?.state === "down"
          ? el("span", { class: "tool-store-badge blocked", text: "Not responding" })
          : el("span", { class: "tool-store-badge" + (connected ? "" : " muted"), text: connected ? "Connected" : server.added ? "Needs sign-in" : "Tool server" }),
      ]),
      el("h4", { text: server.name }),
      el("p", { text: server.description }),
      ...(integration ? healthEl(integration) : []),
      el("div", { class: "tool-store-card-foot" }, [
        server.docs ? el("a", { href: server.docs, target: "_blank", rel: "noopener", text: "Documentation" }) : el("span"),
        actionHost,
      ]),
    ]);
  }

  function customToolCard(item) {
    const connected = item.auth !== "oauth" || item.signed_in;
    const actionHost = el("div", { class: "tool-store-action" });
    if (!connected) actionHost.append(el("button", { type: "button", class: "btn", text: "Sign in", onclick: () => signIn(item.id, item.name, actionHost) }));
    else actionHost.append(el("span", { class: "tool-store-badge", text: "Added" }));
    return el("article", { class: "tool-store-card" }, [
      el("div", { class: "tool-store-card-top" }, [
        el("span", { class: "tool-store-mark" }, [svg(ICONS.store)]),
        item.status?.state === "down"
          ? el("span", { class: "tool-store-badge blocked", text: "Not responding" })
          : el("span", { class: "tool-store-badge" + (connected ? "" : " muted"), text: connected ? "Connected" : "Needs sign-in" }),
      ]),
      el("h4", { text: item.name }),
      el("p", { text: item.url || (item.command ? `Local command: ${item.command}` : "Custom MCP server") }),
      ...healthEl(item),
      el("div", { class: "tool-store-card-foot" }, [
        el("button", { type: "button", class: "btn quiet", text: "Manage", onclick: () => navigate("settings", { section: "integrations" }) }),
        actionHost,
      ]),
    ]);
  }

  async function signIn(id, name, host) {
    let started;
    try { started = await api(`/api/integrations/${id}/oauth/start`, { method: "POST" }); }
    catch (error) { if (host === serverStatus) host.textContent = error.message; draw(); return; }
    if (started.signed_in) {
      await reloadConnections();
      if (host === serverStatus) host.textContent = `Signed in to ${name}.`;
      return;
    }
    window.open(started.url, "_blank", "noopener");
    host.replaceChildren(
      el("span", { class: "meta", text: "Waiting for sign-in…" }),
      el("a", { href: started.url, target: "_blank", rel: "noopener", text: "Open sign-in" }),
    );
    const deadline = Date.now() + 5 * 60 * 1000;
    let outcome = "Sign-in timed out. Try again from this server's card.";
    while (Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, 2000));
      if (!host.isConnected) return;
      let status;
      try { status = await api(`/api/integrations/${id}/oauth`); } catch { continue; }
      if (status.signed_in) { outcome = `Signed in to ${name}.`; toast(`Signed in to ${name}`, "success"); break; }
      if (!status.pending) {
        outcome = status.error ? `Sign-in failed: ${status.error}` : "Sign-in did not finish";
        toast(outcome, "error"); break;
      }
    }
    await reloadConnections();
    if (host === serverStatus) host.textContent = outcome;
  }

  async function reloadConnections() {
    try { state.integrations = await api("/api/integrations"); }
    catch { /* The catalog remains visible even if connection status cannot refresh. */ }
    if (container.isConnected) draw();
  }
}

function navigate(tab, options = {}) {
  document.dispatchEvent(new CustomEvent("jarvis:navigate", { detail: { tab, ...options } }));
}

function svg(markup) {
  const span = document.createElement("span");
  span.innerHTML = markup;
  return span.firstElementChild;
}
