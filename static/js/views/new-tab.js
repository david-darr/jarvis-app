// "+" New Tab builder (Developer Mode, David's ask 2026-09-01) — a real
// fill-in-the-blanks form ("title page") that assembles a well-structured
// build request, then hands off into a real streaming chat conversation to
// actually build it (core/custom_tabs.py + the build-custom-tab Skill do
// the rest). Deliberately reuses the real Chat UI for the actual build
// conversation rather than a second bespoke chat renderer — see the
// handoff at the bottom of this file and chat.js's matching pending-handoff
// check in render().
import { api, el, customSelect, toast } from "../api.js";

function modelLabel(ep) {
  return ep.kind === "claude_cli" ? `${ep.name} (${ep.model || "CLI default"})` : `${ep.name} (${ep.model})`;
}

const DATA_SOURCES = ["Gmail", "Calendar", "Canvas / School", "Custom API"];

// Same convention slashCommands.js already uses to switch tabs from outside
// app.js — click the real nav item rather than importing app.js (no
// existing module does that, so this matches, not invents, a pattern).
function switchTab(tabId) {
  const navItem = document.querySelector(`.nav-item[data-tab="${tabId}"]`);
  if (navItem) navItem.click();
}

export async function render(container) {
  container.innerHTML = "";
  container.classList.add("new-tab-view");

  const header = el("div", { class: "new-tab-header" }, [
    el("h1", { text: "New Tab" }),
    el("p", { text: "Add a ready-made tab, or describe your own and JARVIS builds it with your connected AI model." }),
  ]);

  // Premade tabs (David's ask 2026-09-03) — real, already-built tabs that
  // ship dormant with the app. One click switches one on, versus asking a
  // model to rebuild something equivalent from scratch.
  const templatesWrap = el("div", { class: "new-tab-form", style: "margin-bottom:26px;" });
  renderTemplates(templatesWrap);

  // Real bug found live 2026-09-02: the handoff session had no model_endpoint_id
  // (sessions ship with none by default — see chat_service.py's
  // NO_MODEL_MESSAGE), so hitting "Build my tab" silently sent the seeded
  // message into a session that could never do any real work, just the
  // canned "you haven't added a model yet" reply. Fixed by picking a model
  // here, before the session is even created, and setting it explicitly.
  const endpoints = await api("/api/models").catch(() => []);

  const nameInput = el("input", { placeholder: "Tab name, e.g. \"School\"" });
  const iconInput = el("input", { placeholder: "Icon idea (optional) — e.g. \"graduation cap\"" });
  const whatText = el("textarea", { rows: "4", placeholder: "What should this tab do?" });

  const selectedSources = new Set();
  const chipsWrap = el("div", { class: "new-tab-chips" });
  for (const src of DATA_SOURCES) {
    const chip = el("button", { type: "button", class: "new-tab-chip", text: src });
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
  const savesDataRow = el("label", { class: "new-tab-checkbox-row" }, [
    savesDataCheckbox,
    el("span", { text: "This tab needs to save its own data" }),
  ]);

  const exampleText = el("textarea", {
    rows: "3",
    placeholder: "Walk me through an example of using this tab (optional, but helps a lot)",
  });
  const lookFeelInput = el("input", { placeholder: "Look & feel reference (optional) — e.g. \"like the Tasks tab\"" });

  const modelOptions = endpoints.map((ep) => el("option", { value: ep.id, text: modelLabel(ep) }));
  const modelSelect = endpoints.length
    ? customSelect({ style: "width:100%;" }, modelOptions)
    : null;
  const modelField = el("div", { class: "new-tab-field" }, [
    el("label", { text: "Model to build it" }),
    modelSelect || el("div", { class: "new-tab-error", text: "No models added yet — add one in Settings > Add Models first." }),
  ]);

  const errorMsg = el("div", { class: "new-tab-error hidden" });
  const buildBtn = el("button", { type: "button", class: "btn new-tab-build-btn", text: "Build my tab" });
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

    const lines = [`Build me a new jarvis-app tab called "${name}".`, "", `What it should do: ${what}`];
    if (iconInput.value.trim()) lines.push(`Icon idea: ${iconInput.value.trim()}`);
    if (sources.length) lines.push(`Data sources it should use: ${sources.join(", ")}`);
    if (savesDataCheckbox.checked) {
      const detail = savesDataDetail.value.trim();
      lines.push(`It needs to save its own data${detail ? `: ${detail}` : "."}`);
    }
    if (exampleText.value.trim()) lines.push(`Example of how I'd use it: ${exampleText.value.trim()}`);
    if (lookFeelInput.value.trim()) lines.push(`Look and feel reference: ${lookFeelInput.value.trim()}`);
    lines.push(
      "",
      "Use the build-custom-tab Skill for the exact file convention (routes/tab_<slug>.py with a " +
        "router + TAB_MANIFEST, static/js/views/<slug>.js, optional services/<slug>_service.py) so it's " +
        "auto-discovered with no other file edits. Tell me when it's ready and that the server needs a restart to pick it up.",
    );
    const message = lines.join("\n");

    buildBtn.disabled = true;
    buildBtn.textContent = "Starting...";
    try {
      const session = await api("/api/sessions", { method: "POST", body: JSON.stringify({}) });
      await api(`/api/sessions/${session.id}/model`, {
        method: "POST",
        body: JSON.stringify({ model_endpoint_id: modelSelect.value }),
      });
      // Consumed once by chat.js's render() — the one deliberate exception
      // to "Chat always lands on the welcome screen" (see its own comment).
      sessionStorage.setItem("jarvis:pendingChatHandoff", JSON.stringify({ sessionId: session.id, message }));
      switchTab("chat");
    } catch (e) {
      errorMsg.textContent = `Couldn't start: ${e.message}`;
      errorMsg.classList.remove("hidden");
      buildBtn.disabled = false;
      buildBtn.textContent = "Build my tab";
    }
  });

  const form = el("div", { class: "new-tab-form" }, [
    el("div", { class: "new-tab-field" }, [el("label", { text: "Tab name" }), nameInput]),
    el("div", { class: "new-tab-field" }, [el("label", { text: "Icon idea" }), iconInput]),
    el("div", { class: "new-tab-field" }, [el("label", { text: "What should this tab do?" }), whatText]),
    el("div", { class: "new-tab-field" }, [el("label", { text: "Data sources" }), chipsWrap, otherSourceInput]),
    el("div", { class: "new-tab-field" }, [savesDataRow, savesDataDetail]),
    el("div", { class: "new-tab-field" }, [el("label", { text: "Example use" }), exampleText]),
    el("div", { class: "new-tab-field" }, [el("label", { text: "Look & feel" }), lookFeelInput]),
    modelField,
    errorMsg,
    buildBtn,
  ]);

  container.append(header, templatesWrap, form);
}

async function renderTemplates(wrap) {
  const templates = await api("/api/system/tab-templates").catch(() => []);
  wrap.innerHTML = "";
  if (!templates.length) return;

  wrap.append(
    el("div", { class: "new-tab-section-title", text: "Ready-made tabs" }),
    el("div", { class: "meta", style: "margin:-4px 0 12px;line-height:1.6;", text: "Built and ready — add one and it appears in your sidebar straight away." }),
  );

  for (const t of templates) {
    const btn = el("button", {
      class: t.enabled ? "btn danger" : "btn",
      text: t.enabled ? "Remove" : "Add",
    });
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      btn.textContent = t.enabled ? "Removing…" : "Adding…";
      try {
        await api(`/api/system/tab-templates/${t.slug}`, {
          method: "POST",
          body: JSON.stringify({ enabled: !t.enabled }),
        });
        toast(
          t.enabled ? `${t.label} removed` : `${t.label} added — it's in your sidebar now`,
          "success",
        );
        // Rebuild the sidebar so the tab appears/disappears without a reload.
        // Same "click the real nav item" convention used by switchTab above:
        // dispatch a custom event app.js listens for rather than importing it.
        document.dispatchEvent(new CustomEvent("jarvis:tabs-changed"));
        await renderTemplates(wrap);
      } catch (e) {
        btn.disabled = false;
        btn.textContent = t.enabled ? "Remove" : "Add";
      }
    });

    wrap.appendChild(el("div", { class: "glass bracket card template-card" }, [
      el("div", { class: "card-row", style: "align-items:flex-start;" }, [
        el("div", { style: "flex:1;min-width:0;" }, [
          el("div", { class: "title", text: t.label + (t.enabled ? " · added" : "") }),
          el("div", { class: "meta", style: "margin-top:4px;line-height:1.55;", text: t.blurb }),
          t.detail ? el("div", { class: "meta", style: "margin-top:6px;color:var(--text-faint);line-height:1.55;", text: t.detail }) : null,
        ]),
        btn,
      ]),
    ]));
  }
}
