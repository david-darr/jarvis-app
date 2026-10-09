import { api, el, toast, confirmDialog, customSelect, openPanelDialog } from "../api.js";
import { ICONS } from "../icons.js";
import { pill } from "../settingsKit.js";
import { shareButton, githubStrip } from "../storePublish.js";
import { builderMessage, startBuilderChat, openBuilderBrief } from "../builderBrief.js";

// Browse what Kairos can use. Skill content and live MCP tool schemas stay
// behind their own read/describe calls; this view only loads catalog metadata.
export async function render(container) {
  container.innerHTML = "";
  const state = { query: "", kind: "all", installed: false, skills: [], servers: [], integrations: [], skillsError: "", catalogError: "", tabs: [], tabsError: "", admin: false, community: [], communityError: "" };
  const search = el("input", { type: "search", class: "tool-store-search", placeholder: "Search skills, tools and tabs", "aria-label": "Search skills, tools and tabs" });
  const filters = el("div", { class: "segmented-tabs tool-store-filters", role: "group", "aria-label": "Store category" });
  const installed = el("label", { class: "tool-store-installed" }, [
    el("input", { type: "checkbox" }), el("span", { text: "Added only" }),
  ]);
  const results = el("div", { class: "tool-store-results" });
  const githubHost = el("div", { class: "store-github" });
  const count = el("span", { class: "meta", "aria-live": "polite" });
  const manageSkills = el("button", { type: "button", class: "btn quiet", text: "Manage skills" });
  const manageTools = el("button", { type: "button", class: "btn quiet", text: "Add MCP server", disabled: true });
  const buildTab = el("button", { type: "button", class: "btn primary", text: "Build a tab", hidden: true, "aria-haspopup": "dialog" });
  const installTab = el("button", { type: "button", class: "btn quiet", text: "Install a tab", hidden: true, "aria-haspopup": "dialog" });
  const refreshCommunity = el("button", { type: "button", class: "btn quiet", text: "Refresh community", hidden: true, onclick: async () => {
    refreshCommunity.disabled = true;
    try { setCommunity(await api("/api/store/refresh", { method: "POST" })); draw(); }
    catch (error) { toast(error.message, "error"); }
    finally { refreshCommunity.disabled = false; }
  } });
  const remoteInstall = el("button", { type: "button", class: "btn quiet", text: "Install a single-file skill from GitHub", hidden: true, "aria-haspopup": "dialog" });
  const builders = [
    { kind: "skills", brief: "skill", title: "Skill brief" },
    { kind: "tools", brief: "mcp", title: "Tool brief" },
    { kind: "automations", brief: "automation", title: "Automation brief" },
  ].map(config => {
    const button = el("button", { type: "button", class: "btn primary", text: "Build with Kairos", hidden: true,
      "data-build-kind": config.kind, "aria-haspopup": "dialog" });
    button.addEventListener("click", () => openBuilderBrief(button, config.brief, config.title, container));
    return { ...config, button };
  });
  const popup = (title, body, opener, onClose) => openPanelDialog({ title, body, opener, owner: view,
    wide: true, group: "tool-store", onClose });
  manageSkills.setAttribute("aria-haspopup", "dialog");
  manageTools.setAttribute("aria-haspopup", "dialog");
  manageSkills.addEventListener("click", () => showSkillManager());
  remoteInstall.addEventListener("click", setupSkillInstaller);
  manageTools.addEventListener("click", setupCustomServer);
  // Both tab-building buttons open the same brief.
  const openTabBuilder = event => {
    const body = el("div");
    const dialog = popup("Build a tab", body, event.currentTarget);
    renderTabBuilder(body, dialog);
  };
  buildTab.addEventListener("click", openTabBuilder);
  installTab.addEventListener("click", setupTabInstaller);

  container.append(el("div", { class: "view-constrained tool-store-view" }, [
    el("div", { class: "view-header" }, [
      el("div", {}, [
        el("h2", { text: "Tool Store" }),
        el("div", { class: "sub", text: "Find skills, connect tools and add tabs. Choose connected servers in each chat's Integrations menu." }),
      ]),
    ]),
    el("div", { class: "tool-store-toolbar" }, [search, filters, installed]),
    el("div", { class: "tool-store-summary" }, [count, el("div", { class: "tool-store-manage" }, [manageSkills, manageTools, remoteInstall, ...builders.map(b => b.button), buildTab, installTab, refreshCommunity])]),
    results,
  ]));
  const view = container.firstElementChild;
  results.append(el("div", { class: "tool-store-empty", role: "status", text: "Loading skills and tools…" }));

  search.addEventListener("input", () => { state.query = search.value.trim().toLowerCase(); draw(); });
  installed.querySelector("input").addEventListener("change", (event) => {
    state.installed = event.target.checked;
    draw();
  });
  const loaded = await Promise.allSettled([
    api("/api/skills"), api("/api/integrations/catalog"), api("/api/integrations"), api("/api/system/tabs"), api("/api/auth/status"), api("/api/store/catalog"),
  ]);
  if (!view.isConnected) return;
  if (loaded[0].status === "fulfilled") state.skills = loaded[0].value;
  else state.skillsError = "Skills are unavailable right now.";
  if (loaded[1].status === "fulfilled") state.servers = loaded[1].value;
  else state.catalogError = loaded[1].reason?.message?.startsWith("403:")
    ? "Only an admin can browse and add tool connections."
    : "The tool catalog is unavailable right now.";
  if (loaded[2].status === "fulfilled") state.integrations = loaded[2].value;
  if (loaded[1].status === "fulfilled") {
    remoteInstall.hidden = false;
    manageTools.disabled = false;
  } else if (state.catalogError.startsWith("Only an admin")) manageTools.remove();
  if (loaded[3].status === "fulfilled") state.tabs = loaded[3].value;
  else state.tabsError = "Tabs are unavailable right now.";
  state.admin = loaded[4].status === "fulfilled" && loaded[4].value.is_admin;
  if (loaded[5].status === "fulfilled") setCommunity(loaded[5].value);
  else if (state.admin) state.communityError = loaded[5].reason?.message || "The community catalog is unavailable right now.";
  refreshCommunity.hidden = !state.admin;
  draw();
  if (state.admin) githubStrip(githubHost);

  async function showSkillManager(focusSlug = null, opener = manageSkills) {
    const managerHost = el("div", { class: "tool-store-manager" });
    const dialog = popup(focusSlug ? `Manage skill: ${focusSlug}` : "Manage skills", managerHost, opener, async () => {
      if (!dialog.changed || !view.isConnected) return;
      try { state.skills = await api("/api/skills"); state.skillsError = ""; }
      catch { state.skillsError = "Skills could not refresh. Reopen the store."; }
      if (view.isConnected) draw();
    });
    try {
      const { renderSkillManager } = await import("../skillManager.js");
      if (!managerHost.isConnected) return;
      await renderSkillManager(managerHost, focusSlug, dialog);
    } catch (error) { if (managerHost.isConnected) managerHost.textContent = error.message; }
  }

  function setupSkillInstaller(event) {
    const remoteInstall = el("div", { class: "tool-store-install" });
    const dialog = popup("Install a single-file skill from GitHub", remoteInstall, event.currentTarget);
    const sourceInput = el("input", { type: "url", placeholder: "https://github.com/owner/repo/blob/main/path/SKILL.md", "aria-label": "GitHub SKILL.md URL", required: true });
    const installButton = el("button", { type: "button", class: "btn", text: "Install skill" });
    const installStatus = el("div", { class: "meta", role: "status" });
    const installReview = el("div", { class: "tool-store-review" });
    remoteInstall.append(
      el("p", { class: "meta", text: "Paste a public SKILL.md file link. Kairos scans it before adding it; a flagged skill may need your review. Companion files are not imported." }),
      el("div", { class: "tool-store-form-row" }, [sourceInput, installButton]),
      installStatus, installReview,
    );
    installButton.addEventListener("click", () => installSkill(false));

    async function installSkill(confirmed, expectedSha256 = null) {
      const url = sourceInput.value.trim();
      if (!url) { sourceInput.focus(); return; }
      installButton.disabled = true;
      installStatus.textContent = "Downloading and scanning…";
      installReview.replaceChildren();
      try {
        const response = await fetch("/api/skills/install-url", {
          signal: dialog.signal, method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ url, confirmed, expected_sha256: expectedSha256 }),
        });
        const payload = await response.json().catch(() => ({}));
        if (!remoteInstall.isConnected) return;
        if (response.ok) {
          toast(`Installed ${payload.slug}`, "success");
          sourceInput.value = "";
          installStatus.textContent = "Skill installed.";
          try { state.skills = await api("/api/skills", { signal: dialog.signal }); state.skillsError = ""; }
          catch { if (dialog.signal.aborted) return; state.skillsError = "Skill installed, but the list could not refresh. Reopen the store."; }
          dialog.close();
          if (view.isConnected) draw();
          return;
        }
        const detail = payload.detail;
        installStatus.textContent = typeof detail === "string" ? detail : "Review the scan below.";
        if (response.status === 409 && detail && typeof detail === "object") {
          showInstallReview(installReview, detail, "skill", () => installSkill(true, detail.sha256));
        }
      } catch (error) { if (!remoteInstall.isConnected) return; installStatus.textContent = `Install failed: ${error.message}`; }
      finally { if (remoteInstall.isConnected) installButton.disabled = false; }
    }
  }

  function setupCustomServer(event) {
    const customServer = el("div", { class: "tool-store-install" });
    const dialog = popup("Add a custom MCP server", customServer, event.currentTarget);
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
      el("p", { class: "meta", text: "Connect a third-party HTTP MCP endpoint. Its tools still use Kairos's permission prompts and are selected per chat." }),
      el("div", { class: "tool-store-form-row" }, [serverName, serverUrl, serverAuth, serverKey, serverButton]),
      serverStatus,
      el("button", { type: "button", class: "btn quiet", text: "Advanced connection settings", onclick: () => { dialog.close(); navigate("settings", { section: "integrations" }); } }),
    );
    serverButton.addEventListener("click", addServer);

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
          signal: dialog.signal, method: "POST", body: JSON.stringify({ name, mcp_type: "http", url,
            ...(serverAuth.value === "oauth" ? { auth: "oauth" } : {}),
            ...(serverAuth.value === "key" ? { api_key: serverKey.value.trim() } : {}) }),
        });
        if (!customServer.isConnected) return;
        state.integrations.push(item);
        const catalogMatch = state.servers.find((server) => server.url.replace(/\/$/, "") === item.url.replace(/\/$/, ""));
        if (catalogMatch) catalogMatch.added = true;
        serverName.value = ""; serverUrl.value = ""; serverKey.value = "";
        if (item.auth === "oauth") await signIn(item.id, item.name, serverStatus, dialog);
        else { serverStatus.textContent = "Server added."; toast(`${item.name} added`, "success"); dialog.close(); draw(); }
      } catch (error) { if (!customServer.isConnected) return; serverStatus.textContent = error.message; }
      finally { if (customServer.isConnected) serverButton.disabled = false; }
    }
  }

  function draw() {
    if (!view.isConnected) return;
    filters.innerHTML = "";
    for (const [id, label] of [["all", "All"], ["skills", "Skills"], ["tools", "Tools"], ["tabs", "Tabs"], ["automations", "Automations"]]) {
      const button = el("button", {
        type: "button", class: "segmented-tab" + (state.kind === id ? " active" : ""),
        text: label, "aria-pressed": state.kind === id ? "true" : "false",
      });
      button.addEventListener("click", () => { state.kind = id; draw(); });
      filters.append(button);
    }
    remoteInstall.hidden = !["all", "skills", "tools"].includes(state.kind) || loaded[1].status !== "fulfilled";
    manageSkills.hidden = ["tabs", "automations"].includes(state.kind);
    manageTools.hidden = ["tabs", "automations"].includes(state.kind);
    buildTab.hidden = state.kind !== "tabs";
    for (const builder of builders) {
      builder.button.hidden = !state.admin || state.kind !== builder.kind;
    }
    installTab.hidden = state.kind !== "tabs" || !state.admin;
    results.innerHTML = "";
    const q = state.query;
    const skills = !["all", "skills"].includes(state.kind) ? [] : state.skills.filter((item) =>
      !q || `${item.slug} ${item.description}`.toLowerCase().includes(q));
    const tools = !["all", "tools"].includes(state.kind) ? [] : state.servers.filter((item) =>
      (!q || `${item.name} ${item.description || ""}`.toLowerCase().includes(q)) &&
      (!state.installed || item.added));
    const custom = !["all", "tools"].includes(state.kind) ? [] : state.integrations.filter((item) => item.kind === "mcp_server" &&
      !state.servers.some((server) => server.url.replace(/\/$/, "") === (item.url || "").replace(/\/$/, "")) &&
      (!q || `${item.name} ${item.url || ""}`.toLowerCase().includes(q)));
    const tabs = ["all", "tabs"].includes(state.kind) ? state.tabs.filter((item) =>
      (!q || `${item.slug} ${item.name} ${item.blurb || ""} ${item.description} ${item.detail || ""} ${item.reads || ""} ${item.status} ${item.reason || ""}`.toLowerCase().includes(q)) &&
      (!state.installed || item.kind === "user" || item.enabled)) : [];
    const kindFilter = { skill: "skills", tool: "tools", tab: "tabs", automation: "automations" };
    const community = state.community.filter((item) =>
      (state.kind === "all" || state.kind === kindFilter[item.kind]) &&
      (!state.installed || item.installed) &&
      (!q || `${item.name} ${item.description} ${item.author} ${item.kind}`.toLowerCase().includes(q)));
    const total = skills.length + tools.length + custom.length + tabs.length + community.length;
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
    for (const [kind, label] of [["prebuilt", "Prebuilt"], ["user", "Yours"]]) {
      const entries = tabs.filter((item) => item.kind === kind);
      // Yours always ends with the way to build one, and is that card alone when empty.
      const offerBuild = kind === "user" && state.kind === "tabs" && !q;
      if (!entries.length && !offerBuild) continue;
      results.append(el("h3", { class: "tool-store-heading", text: `${label} · ${entries.length}` }));
      const grid = el("div", { class: "tool-store-grid" });
      for (const item of entries) grid.append(tabCard(item));
      if (offerBuild) grid.append(buildTabCard());
      results.append(grid);
    }
    if (state.admin) {
      results.append(el("h3", { class: "tool-store-heading", text: `Community · ${community.length}`, "data-community-heading": "" }));
      results.append(githubHost);
      if (state.communityError) results.append(el("div", { class: "tool-store-notice", role: "status", text: state.communityError }));
      const grid = el("div", { class: "tool-store-grid tool-store-community" });
      for (const item of community) grid.append(communityCard(item));
      results.append(grid);
    }
    if (["all", "tabs"].includes(state.kind) && state.tabsError) results.append(el("div", { class: "tool-store-notice", text: state.tabsError }));
    if (["all", "tools"].includes(state.kind) && state.catalogError) {
      results.append(el("div", { class: "tool-store-notice", text: state.catalogError }));
    }
    if (["all", "skills"].includes(state.kind) && state.skillsError) {
      results.append(el("div", { class: "tool-store-notice", text: state.skillsError }));
    }
    if (!total && !state.tabsError && !(["all", "tools"].includes(state.kind) && state.catalogError) && !(["all", "skills"].includes(state.kind) && state.skillsError)) {
      results.append(el("div", { class: "tool-store-empty", text: q ? "No matches. Try another search." : "Nothing in this view yet." }));
    }
  }

  async function refreshTabs(signal) {
    document.dispatchEvent(new CustomEvent("jarvis:tabs-changed"));
    state.tabs = await api("/api/system/tabs", { signal });
    if (!view.isConnected) return;
    state.tabsError = "";
    draw();
  }

  function setCommunity(catalog) {
    state.community = catalog.items || [];
    state.communityError = catalog.stale ? `Showing the saved catalog. ${catalog.error || "Refresh when the connection returns."}` : "";
  }

  async function reloadCommunity() {
    const values = await Promise.allSettled([api("/api/store/catalog"), api("/api/system/tabs"), api("/api/skills"), api("/api/integrations")]);
    if (values[0].status === "fulfilled") setCommunity(values[0].value);
    if (values[1].status === "fulfilled") state.tabs = values[1].value;
    if (values[2].status === "fulfilled") state.skills = values[2].value;
    if (values[3].status === "fulfilled") state.integrations = values[3].value;
    document.dispatchEvent(new CustomEvent("jarvis:tabs-changed"));
    draw();
  }

  // The local import and Community paths share the same review presentation.
  function showInstallReview(host, detail, kind, confirm) {
    host.replaceChildren(el("div", { class: "tool-store-scan" }, [
      el("div", { class: "title", text: detail.needs_confirmation ? `Review this ${kind}` : "Installation blocked" }),
      el("pre", { text: detail.report || "" }),
      ...(detail.fingerprint ? [el("p", { class: "meta tool-store-tab-hash", text: `SHA-256: ${detail.fingerprint}` })] : []),
      ...(detail.needs_confirmation ? [el("button", { type: "button", class: "btn danger", text: "Install anyway", onclick: confirm })] : []),
    ]));
  }

  function communityCard(item) {
    const review = el("div", { class: "tool-store-review" });
    const status = item.turned_off ? "Turned off" : item.update_available ? "Update available" : item.installed ? "Installed" : item.install_blocked ? "Revoked" : "Community";
    const actions = el("div", { class: "tool-store-card-foot" });
    const button = el("button", { type: "button", class: "btn primary", text: item.update_available ? "Update" : "Install", disabled: !!item.install_blocked });
    async function installCommunity(options = {}) {
      if (item.installed && !options.confirmed && !await confirmDialog({ title: `Update ${item.name}?`, message: "Replace this store item's installed source with the catalog version. Tabs need source approval again. Automations install turned off.", confirmLabel: "Update" })) return;
      button.disabled = true; review.replaceChildren();
      try {
        const response = await fetch("/api/store/install", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ kind: item.kind, slug: item.slug, replace: !!item.installed, ...options }) });
        const payload = await response.json();
        if (response.status === 409 && typeof payload.detail === "object") {
          const detail = payload.detail;
          showInstallReview(review, detail, item.kind, () => installCommunity({ confirmed: true,
            expected_fingerprint: detail.fingerprint, expected_sha256: detail.sha256, expected_commit: detail.commit }));
          return;
        }
        if (!response.ok) throw new Error(typeof payload.detail === "string" ? payload.detail : "Installation failed.");
        toast(item.kind === "tab" ? "Installed. Review and approve its files in Yours." : item.kind === "automation" ? "Installed turned off. Choose delivery and a model in Tasks." : `Installed ${item.name}`, "success");
        if (item.kind === "tab") state.kind = "tabs";
        await reloadCommunity();
      } catch (error) { review.textContent = error.message; }
      finally { button.disabled = !!item.install_blocked; }
    }
    button.addEventListener("click", () => installCommunity());
    if (!item.installed || item.update_available) actions.append(button);
    if (item.turned_off) actions.append(el("button", { type: "button", class: "btn quiet", text: "Turn back on", onclick: async () => {
      if (!await confirmDialog({ title: `Turn ${item.name} back on?`, message: `This item was pulled from the store:\n\n${item.revoked_reason}\n\nExisting source approval and tool acceptance still apply.`, confirmLabel: "Turn back on", danger: true })) return;
      try {
        await api(`/api/store/revoked/${item.kind}/${encodeURIComponent(item.slug)}/reenable`, { method: "POST", body: JSON.stringify({ confirmed: true, expected_revocation: item.revoked_reason }) });
        await reloadCommunity();
      } catch (error) { toast(error.message, "error"); }
    } }));
    if (item.installed) {
      actions.append(el("button", { type: "button", class: "btn quiet", text: "Manage", "aria-haspopup": item.kind === "skill" ? "dialog" : "false", onclick: event => {
        if (item.kind === "tab") { state.kind = "tabs"; draw(); }
        else if (item.kind === "skill") showSkillManager(item.local_id, event.currentTarget);
        else navigate(item.kind === "automation" ? "tasks" : "settings", item.kind === "tool" ? { section: "integrations" } : {});
      } }));
      actions.append(el("button", { type: "button", class: "btn quiet danger", text: "Remove", onclick: async () => {
        if (!await confirmDialog({ title: `Remove ${item.name}?`, message: "Remove this community item from Kairos. Saved tab data is kept.", confirmLabel: "Remove" })) return;
        try { await api(`/api/store/installed/${item.kind}/${encodeURIComponent(item.slug)}`, { method: "DELETE" }); await reloadCommunity(); }
        catch (error) { toast(error.message, "error"); }
      } }));
    }
    return el("article", { class: "tool-store-card", "data-community-kind": item.kind, "data-community-slug": item.slug }, [
      el("div", { class: "tool-store-card-top" }, [el("span", { class: "tool-store-mark" }, [svg(ICONS.store)]),
        el("span", { class: "tool-store-badge" + (item.turned_off || item.install_blocked ? " blocked" : ""), text: status })]),
      el("h4", { text: item.name }), el("p", { text: item.description }),
      el("div", { class: "meta", text: `Community · ${item.kind} · @${item.author} · v${item.version}` }),
      item.revoked_reason ? el("p", { class: "tool-store-tab-reason", text: item.revoked_reason }) : null,
      actions, review,
    ]);
  }

  function buildTabCard() {
    return el("div", { class: "tool-store-card tool-store-build-card" }, [
      el("h4", { text: "Build your own tab" }),
      el("p", { text: "Describe what it should do and Kairos builds it in a chat. It lives in your data folder, survives updates and runs once an admin approves it." }),
      el("div", { class: "tool-store-card-foot" }, [
        el("button", { type: "button", class: "btn primary", text: "Start building", "aria-haspopup": "dialog", onclick: openTabBuilder }),
      ]),
    ]);
  }

  function tabCard(item) {
    const slug = encodeURIComponent(item.slug);
    const prebuilt = item.kind === "prebuilt";
    const status = ({ needs_approval: "Needs approval", needs_newer_kairos: "Needs a newer Kairos",
      on: "On", off: "Off", invalid: "Invalid", failed: "Failed" })[item.status] || item.status;
    const actions = el("div", { class: "tool-store-card-foot" });
    let reviewDialog = null;
    const reviewStatus = el("div", { class: "meta", role: "status" });
    const reviewBody = () => el("div", { class: "tool-store-tab-review" }, [
      el("p", { class: "meta", text: item.format === "legacy"
        ? "Legacy approval covers the shared legacy source tree. Inspect these files in your Kairos data folder."
        : `Inspect these files in data/tabs/${item.slug}/. Approval lets this code run inside Kairos.` }),
      el("pre", { class: "tool-store-tab-files", text: (item.files || []).join("\n") || "No files available." }),
      el("p", { class: "meta tool-store-tab-hash", text: `SHA-256: ${item.fingerprint || "Unavailable"}` }),
      reviewStatus,
    ]);
    const review = el("button", { type: "button", class: "btn quiet", text: "Review files", "aria-haspopup": "dialog" });
    review.addEventListener("click", () => {
      const home = actions.parentNode;
      reviewStatus.textContent = "";
      reviewDialog = popup(`Review files: ${item.name}`, reviewBody(), review, () => {
        home?.append(actions);
        reviewDialog = null;
      });
      reviewDialog.panel.append(el("div", { class: "modal-footer" }, [actions]));
    });
    const action = (label, fn, attrs = {}) => el("button", { type: "button", class: "btn quiet", text: label,
      ...attrs, onclick: async (event) => {
        const button = event.currentTarget;
        button.disabled = true;
        try { await fn(reviewDialog); } catch (error) {
          if (error.name === "AbortError") return;
          if (reviewStatus.isConnected) reviewStatus.textContent = error.message;
          toast(error.message, "error");
        }
        finally { if (button.isConnected) button.disabled = false; }
      } });
    if (state.admin) {
      if (prebuilt) actions.append(action(item.enabled ? "Remove" : "Add", async () => {
        const result = await api(`/api/system/tab-templates/${slug}`, { method: "POST", body: JSON.stringify({ enabled: !item.enabled }) });
        toast(result.restart_required
          ? (item.enabled ? "Tab is off. Restart Kairos to fully unload it." : "Tab enabled. Restart Kairos to load the current version.")
          : (item.enabled ? "Tab is off." : "Tab added."), "success");
        await refreshTabs();
      }, { class: item.enabled ? "btn quiet danger" : "btn primary", disabled: !item.enabled && ["invalid", "needs_newer_kairos"].includes(item.status) }));
      else {
        if (item.status !== "on") actions.append(action("Approve", async (dialog) => {
          const ok = await confirmDialog({ title: `Approve ${item.name}?`, danger: false, confirmLabel: "Approve source",
            message: `Inspect all files before allowing this code to run inside Kairos.\n\n${(item.files || []).join("\n")}\n\nFingerprint: ${item.fingerprint}` });
          if (!ok) return;
          if (!actions.isConnected || dialog?.signal.aborted) return;
          const result = await api(`/api/system/custom-tabs/${slug}/approve`, { method: "POST", body: JSON.stringify({ fingerprint: item.fingerprint }), signal: dialog?.signal });
          if (dialog?.signal.aborted) return;
          toast(result.restart_required ? "Approved. Restart Kairos to unload the previous version and load this tab." : "Tab approved and added.", "success");
          await refreshTabs(dialog?.signal);
          dialog?.close();
        }, { class: "btn primary", disabled: !item.fingerprint || ["invalid", "needs_newer_kairos"].includes(item.status) }));
        actions.append(action("Remove", async (dialog) => {
          if (!await confirmDialog({ title: `Remove ${item.name}?`, message: "This removes the tab's source files. Its saved data is kept.", confirmLabel: "Remove tab" })) return;
          if (!actions.isConnected || dialog?.signal.aborted) return;
          const result = await api(`/api/system/custom-tabs/${slug}`, { method: "DELETE", signal: dialog?.signal });
          if (dialog?.signal.aborted) return;
          toast(result.restart_required ? "Tab removed. Restart Kairos to fully unload it." : "Tab removed.", "success");
          await refreshTabs(dialog?.signal);
          dialog?.close();
        }, { class: "btn quiet danger" }));
        if (item.format === "folder") actions.append(action("Export", async (dialog) => {
          const response = await api(`/api/system/tabs/${slug}/export`, { signal: dialog?.signal });
          const url = URL.createObjectURL(await response.blob());
          if (!actions.isConnected || dialog?.signal.aborted) { URL.revokeObjectURL(url); return; }
          const link = el("a", { href: url, download: `${item.slug}.kairostab` });
          link.click();
          const signal = dialog?.signal;
          const release = () => { clearTimeout(timer); URL.revokeObjectURL(url); signal?.removeEventListener("abort", release); };
          const timer = setTimeout(release, 1000);
          signal?.addEventListener("abort", release, { once: true });
        }));
        if (item.format === "folder") actions.append(shareButton("tab", item.slug));
      }
    }
    return el("article", { class: "tool-store-card", "data-tab-slug": item.slug, "data-tab-kind": item.kind }, [
      el("div", { class: "tool-store-card-top" }, [pill(status,
        ({ on: "ok", off: "muted", needs_approval: "warn", needs_newer_kairos: "warn", invalid: "error", failed: "error" })[item.status] || "muted")]),
      el("h4", { text: item.name }), el("p", { text: item.blurb || item.description }),
      item.detail ? el("p", { class: "meta", text: item.detail }) : null,
      item.reads ? el("p", { class: "meta", text: item.reads }) : null,
      item.reason ? el("p", { class: "tool-store-tab-reason", text: item.reason.replace(/\s+/g, " "), title: item.reason }) : null,
      !prebuilt && state.admin ? review : null, actions,
    ]);
  }

  function setupTabInstaller(event) {
    const tabInstaller = el("div", { class: "tool-store-install" });
    const dialog = popup("Install a tab", tabInstaller, event.currentTarget);
    const file = el("input", { type: "file", accept: ".kairostab", "aria-label": "Tab archive" });
    const url = el("input", { type: "url", placeholder: "https://github.com/owner/repo/tree/main/path", "aria-label": "GitHub tab folder" });
    const replace = el("input", { type: "checkbox" });
    const status = el("div", { class: "meta", role: "status" });
    const review = el("div", { class: "tool-store-review" });
    const upload = el("button", { type: "button", class: "btn", text: "Install file", onclick: () => install("file") });
    const github = el("button", { type: "button", class: "btn", text: "Install GitHub folder", onclick: () => install("github") });
    tabInstaller.append(
      el("p", { class: "meta", text: "Import a folder tab. Kairos scans it first; installed code awaits your approval in Yours." }),
      el("div", { class: "tool-store-form-row" }, [file, upload]),
      el("div", { class: "tool-store-form-row" }, [url, github]),
      el("label", { class: "tab-build-checkbox-row" }, [replace, el("span", { text: "Replace an existing user tab and revoke its approval" })]), status, review);
    async function install(kind, confirmed = false, fingerprint = null, snapshot = null) {
      snapshot ||= { file: file.files[0], url: url.value.trim(), replace: replace.checked };
      if (kind === "file" && !snapshot.file) { file.focus(); return; }
      if (kind === "github" && !snapshot.url) { url.focus(); return; }
      if (snapshot.replace && !confirmed && !await confirmDialog({ title: "Replace existing tab source?", message: "All old source files for this tab will be removed and its approval revoked. Saved tab data is kept.", confirmLabel: "Replace source" })) return;
      if (!tabInstaller.isConnected) return;
      upload.disabled = github.disabled = true;
      status.textContent = "Downloading and scanning…";
      review.replaceChildren();
      try {
        let body, headers;
        if (kind === "file") {
          body = new FormData(); body.append("file", snapshot.file);
          body.append("replace", String(snapshot.replace)); body.append("confirmed", String(confirmed));
          if (fingerprint) body.append("expected_fingerprint", fingerprint);
        } else {
          headers = { "Content-Type": "application/json" };
          body = JSON.stringify({ github_url: snapshot.url, replace: snapshot.replace, confirmed, expected_fingerprint: fingerprint });
        }
        const response = await fetch("/api/system/tabs/install", { method: "POST", headers, body, signal: dialog.signal });
        const payload = await response.json();
        if (!tabInstaller.isConnected) return;
        if (response.ok) {
          status.textContent = `Installed ${payload.slug}. Review and approve it in Yours.`;
          file.value = ""; url.value = "";
          await refreshTabs(dialog.signal);
          dialog.close();
        } else if (response.status === 409 && typeof payload.detail === "object") {
          const detail = payload.detail;
          status.textContent = detail.needs_confirmation ? "Review the scan before installing." : "Installation blocked.";
          showInstallReview(review, detail, "tab", () => install(kind, true, detail.fingerprint, snapshot));
        } else status.textContent = typeof payload.detail === "string" ? payload.detail : "Installation failed.";
      } catch (error) { if (tabInstaller.isConnected) status.textContent = error.message; }
      finally { if (tabInstaller.isConnected) upload.disabled = github.disabled = false; }
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
    const text = integration.enabled === false ? "Turned off"
      : !status ? "Not checked yet"
      : status.state === "working" ? `Working · ${status.tools} tool${status.tools === 1 ? "" : "s"} · checked ${when}`
      : status.state === "signed_out" ? "Not signed in"
      : `Not responding · ${status.error || "no answer"} · checked ${when}`;
    const check = el("button", { type: "button", class: "btn quiet", text: "Check" });
    check.disabled = integration.enabled === false;
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
    const button = el("button", { type: "button", class: "btn quiet", text: "View skill", "aria-haspopup": "dialog" });
    button.addEventListener("click", async () => {
      const detail = el("div", { text: "Loading skill..." });
      const dialog = popup(`View skill: ${item.slug}`, detail, button);
      try {
        const full = await api(`/api/skills/${encodeURIComponent(item.slug)}`, { signal: dialog.signal });
        if (!detail.isConnected) return;
        detail.replaceChildren(
          el("pre", { class: "tool-store-skill-body", text: full.body || "This skill has no body yet." }),
          el("button", { type: "button", class: "btn quiet", text: "Manage skill", "aria-haspopup": "dialog", onclick: event => showSkillManager(item.slug, event.currentTarget) }),
        );
      } catch (error) { if (detail.isConnected) detail.textContent = error.message; }
    });
    const source = item.curation?.source;
    const origin = item.curation?.origin || "";
    const sourceLabel = origin.startsWith("https://raw.githubusercontent.com/")
      ? el("a", { href: origin, target: "_blank", rel: "noopener", text: "GitHub source" })
      : el("span", { class: "meta", text: source === "bundled" ? "Built into Kairos" : source === "imported" ? "Imported skill"
        : source === "recorded" ? "Recorded skill" : "Local skill" });
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
        state.admin && ["user", "imported", "recorded", "unknown"].includes(source) ? shareButton("skill", item.slug) : null,
      ]),
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
        state.admin && integration?.mcp_type === "http" ? shareButton("tool", integration.id) : null,
      ]),
    ]);
  }

  function customToolCard(item) {
    const suspended = item.enabled === false;
    const connected = !suspended && (item.auth !== "oauth" || item.signed_in);
    const actionHost = el("div", { class: "tool-store-action" });
    if (suspended) actionHost.append(el("span", { class: "tool-store-badge blocked", text: "Turned off" }));
    else if (!connected) actionHost.append(el("button", { type: "button", class: "btn", text: "Sign in", onclick: () => signIn(item.id, item.name, actionHost) }));
    else actionHost.append(el("span", { class: "tool-store-badge", text: "Added" }));
    return el("article", { class: "tool-store-card" }, [
      el("div", { class: "tool-store-card-top" }, [
        el("span", { class: "tool-store-mark" }, [svg(ICONS.store)]),
        suspended ? el("span", { class: "tool-store-badge blocked", text: "Turned off" }) : item.status?.state === "down"
          ? el("span", { class: "tool-store-badge blocked", text: "Not responding" })
          : el("span", { class: "tool-store-badge" + (connected ? "" : " muted"), text: connected ? "Connected" : "Needs sign-in" }),
      ]),
      el("h4", { text: item.name }),
      el("p", { text: item.url || (item.command ? `Local command: ${item.command}` : "Custom MCP server") }),
      ...healthEl(item),
      el("div", { class: "tool-store-card-foot" }, [
        el("button", { type: "button", class: "btn quiet", text: "Manage", onclick: () => navigate("settings", { section: "integrations" }) }),
        actionHost,
        state.admin && item.mcp_type === "http" ? shareButton("tool", item.id) : null,
      ]),
    ]);
  }

  async function signIn(id, name, host, dialog = null) {
    let started;
    try { started = await api(`/api/integrations/${id}/oauth/start`, { method: "POST", signal: dialog?.signal }); }
    catch (error) { if (host.isConnected) host.textContent = error.message; return; }
    if (!host.isConnected) return;
    if (started.signed_in) {
      await reloadConnections(dialog?.signal);
      if (host.isConnected) host.textContent = `Signed in to ${name}.`;
      dialog?.close();
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
      if (dialog?.signal.aborted) return;
      await new Promise(resolve => {
        const done = () => { clearTimeout(timer); dialog?.signal.removeEventListener("abort", done); resolve(); };
        const timer = setTimeout(done, 2000);
        dialog?.signal.addEventListener("abort", done, { once: true });
      });
      if (!host.isConnected) return;
      let status;
      try { status = await api(`/api/integrations/${id}/oauth`, { signal: dialog?.signal }); } catch { continue; }
      if (!host.isConnected) return;
      if (status.signed_in) { outcome = `Signed in to ${name}.`; toast(`Signed in to ${name}`, "success"); break; }
      if (!status.pending) {
        outcome = status.error ? `Sign-in failed: ${status.error}` : "Sign-in did not finish";
        toast(outcome, "error"); break;
      }
    }
    await reloadConnections(dialog?.signal);
    if (host.isConnected) host.textContent = outcome;
    if (outcome === `Signed in to ${name}.`) dialog?.close();
  }

  async function reloadConnections(signal) {
    try { state.integrations = await api("/api/integrations", { signal }); }
    catch { /* The catalog remains visible even if connection status cannot refresh. */ }
    if (view.isConnected && !signal?.aborted) draw();
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

function modelLabel(ep) { return `${ep.name} (${ep.model || "CLI default"})`; }
const DATA_SOURCES = ["Gmail", "Calendar", "Canvas / School", "Custom API"];
async function renderTabBuilder(container, dialog) {
  const endpoints = await api("/api/models", { signal: dialog.signal }).catch(() => []);

  if (!container.isConnected) return;
  const nameInput = el("input", { placeholder: "Tab name, e.g. \"School\"" });
  const iconInput = el("input", { placeholder: "Icon idea (optional), e.g. \"graduation cap\"" });
  const whatText = el("textarea", { rows: "4", placeholder: "What should this tab do?" });

  const selectedSources = new Set();
  const chipsWrap = el("div", { class: "tab-build-chips" });
  for (const src of DATA_SOURCES) {
    const chip = el("button", { type: "button", class: "tab-build-chip", text: src });
    chip.addEventListener("click", () => {
      chip.classList.toggle("active");
      if (selectedSources.has(src)) selectedSources.delete(src);
      else selectedSources.add(src);
    });
    chipsWrap.appendChild(chip);
  }
  const otherSourceInput = el("input", { placeholder: "Other data source (optional)" });

  const savesDataCheckbox = el("input", { type: "checkbox" });
  const savesDataDetail = el("input", {
    placeholder: "What kind of items/fields? (optional)",
    style: "display:none;margin-top:8px;",
  });
  savesDataCheckbox.addEventListener("change", () => {
    savesDataDetail.style.display = savesDataCheckbox.checked ? "" : "none";
  });
  const savesDataRow = el("label", { class: "tab-build-checkbox-row" }, [
    savesDataCheckbox,
    el("span", { text: "This tab needs to save its own data" }),
  ]);

  const exampleText = el("textarea", {
    rows: "3",
    placeholder: "Walk me through an example of using this tab (optional, but helps a lot)",
  });
  const lookFeelInput = el("input", { placeholder: "Look & feel reference (optional), e.g. \"like the Tasks tab\"" });

  const modelOptions = endpoints.map((ep) => el("option", { value: ep.id, text: modelLabel(ep) }));
  const modelSelect = endpoints.length
    ? customSelect({ style: "width:100%;" }, modelOptions)
    : null;
  const modelField = el("div", { class: "tab-build-field" }, [
    el("label", { text: "Model to build it" }),
    modelSelect || el("div", { class: "tab-build-error", text: "No models added yet. Add one in Settings > Add Models first." }),
  ]);

  const errorMsg = el("div", { class: "tab-build-error hidden" });
  const buildBtn = el("button", { type: "button", class: "btn tab-build-build-btn", text: "Build my tab" });
  if (!endpoints.length) buildBtn.disabled = true;

  buildBtn.addEventListener("click", async () => {
    const name = nameInput.value.trim();
    const what = whatText.value.trim();
    errorMsg.classList.add("hidden");
    if (!name || !what) {
      errorMsg.textContent = "Tab name and what it should do are both required.";
      errorMsg.classList.remove("hidden");
      return;
    }

    const sources = [...selectedSources];
    if (otherSourceInput.value.trim()) sources.push(otherSourceInput.value.trim());

    const lines = [`Build me a new Kairos tab called "${name}".`, "", `What it should do: ${what}`];
    if (iconInput.value.trim()) lines.push(`Icon idea: ${iconInput.value.trim()}`);
    if (sources.length) lines.push(`Data sources it should use: ${sources.join(", ")}`);
    if (savesDataCheckbox.checked) {
      const detail = savesDataDetail.value.trim();
      lines.push(`It needs to save its own data${detail ? `: ${detail}` : "."}`);
    }
    if (exampleText.value.trim()) lines.push(`Example of how I'd use it: ${exampleText.value.trim()}`);
    if (lookFeelInput.value.trim()) lines.push(`Look and feel reference: ${lookFeelInput.value.trim()}`);
    const message = builderMessage("build-custom-tab", lines[0], lines.slice(2));

    buildBtn.disabled = true;
    buildBtn.textContent = "Starting...";
    try {
      await startBuilderChat(message, modelSelect.value, { signal: dialog.signal, onStarted: dialog.close });
    } catch (e) {
      if (!container.isConnected) return;
      errorMsg.textContent = `Couldn't start: ${e.message}`;
      errorMsg.classList.remove("hidden");
      buildBtn.disabled = false;
      buildBtn.textContent = "Build my tab";
    }
  });

  const form = el("div", { class: "tab-build-form" }, [
    el("div", { class: "tab-build-field" }, [el("label", { text: "Tab name" }), nameInput]),
    el("div", { class: "tab-build-field" }, [el("label", { text: "Icon idea" }), iconInput]),
    el("div", { class: "tab-build-field" }, [el("label", { text: "What should this tab do?" }), whatText]),
    el("div", { class: "tab-build-field" }, [el("label", { text: "Data sources" }), chipsWrap, otherSourceInput]),
    el("div", { class: "tab-build-field" }, [savesDataRow, savesDataDetail]),
    el("div", { class: "tab-build-field" }, [el("label", { text: "Example use" }), exampleText]),
    el("div", { class: "tab-build-field" }, [el("label", { text: "Look & feel" }), lookFeelInput]),
    modelField,
    errorMsg,
    buildBtn,
  ]);

  container.append(form);
}
