import { api, el, customSelect, openPanelDialog } from "./api.js";

export function builderMessage(skill, request, answers) {
  return [`First call read_skill with slug "${skill}" and follow it.`, "", request, ...answers].join("\n");
}

export async function startBuilderChat(message, modelEndpointId, { signal, onStarted } = {}) {
  const session = await api("/api/sessions", { method: "POST", body: JSON.stringify({}), signal });
  await api(`/api/sessions/${session.id}/model`, {
    method: "POST", body: JSON.stringify({ model_endpoint_id: modelEndpointId }), signal,
  });
  if (signal?.aborted) return;
  onStarted?.();
  sessionStorage.setItem("jarvis:pendingChatHandoff", JSON.stringify({ sessionId: session.id, message }));
  document.querySelector('.nav-item[data-tab="chat"]')?.click();
}

const BRIEFS = {
  skill: { skill: "build-skill", request: "Build and save a reusable Kairos skill.", fields: [
    ["What should the skill do?", "Describe the procedure and its result", true],
    ["When should it be used?", "Describe the trigger or situation", true],
    ["Example", "Give an example input and the expected output", true],
  ] },
  mcp: { skill: "build-mcp-server", request: "Find or build an MCP connection for Kairos.", fields: [
    ["What service or tool?", "Describe the service and operations you need", true],
    ["Existing server URL", "Endpoint URL, if known (optional)", false],
    ["Does it need sign-in?", "Yes, no, or unsure", true],
  ] },
  automation: { skill: "build-automation", request: "Create a Kairos automation that can run unattended.", fields: [
    ["What should it do?", "Describe the inputs and expected output", true],
    ["How often or when?", "For example, every day at 06:00 local time", true],
    ["Where should it deliver?", "Tasks tab only, or a configured channel", true],
  ] },
};

export function builderBriefMessage(kind, answers) {
  const config = BRIEFS[kind];
  return builderMessage(config.skill, config.request, answers);
}

// All briefs share the tab builder's classes and one chat handoff.
export async function renderBuilderBrief(container, kind, dialog) {
  const config = BRIEFS[kind];
  const form = el("div", { class: "tab-build-form", "data-builder": kind });
  const inputs = config.fields.map(([label, placeholder, required], index) => {
    const input = el(index === 0 || kind === "skill" ? "textarea" : "input", {
      placeholder, "aria-label": label, required, ...(index === 0 || kind === "skill" ? { rows: "3" } : {}),
    });
    form.append(el("div", { class: "tab-build-field" }, [el("label", { text: label }), input]));
    return input;
  });
  container.append(form);
  const endpoints = await api("/api/models", { signal: dialog?.signal }).catch(() => []);
  if (!container.isConnected) return;
  const model = endpoints.length ? customSelect({ style: "width:100%;", "aria-label": "Model to build it" },
    endpoints.map(ep => el("option", { value: ep.id, text: `${ep.name} (${ep.model || "CLI default"})` }))) : null;
  const error = el("div", { class: "tab-build-error hidden", role: "status" });
  const button = el("button", { type: "button", class: "btn tab-build-build-btn", text: "Build with Kairos", disabled: !model });
  form.append(el("div", { class: "tab-build-field" }, [el("label", { text: "Model to build it" }),
    model || el("div", { class: "tab-build-error", text: "No models added yet. Add one in Settings > Add Models first." })]), error, button);
  // A "fill in" message must not outlive the typing that answers it.
  form.addEventListener("input", () => error.classList.add("hidden"));
  button.addEventListener("click", async () => {
    error.classList.add("hidden");
    const missing = inputs.findIndex((input, index) => config.fields[index][2] && !input.value.trim());
    if (missing !== -1) {
      error.textContent = "Fill in the required fields before starting.";
      error.classList.remove("hidden"); inputs[missing].focus(); return;
    }
    const answers = inputs.flatMap((input, index) => input.value.trim()
      ? [`${config.fields[index][0]} ${input.value.trim()}`] : []);
    button.disabled = true; button.textContent = "Starting...";
    try { await startBuilderChat(builderBriefMessage(kind, answers), model.value, { signal: dialog?.signal, onStarted: dialog?.close }); }
    catch (e) {
      if (!container.isConnected) return;
      error.textContent = `Couldn't start: ${e.message}`; error.classList.remove("hidden");
      button.disabled = false; button.textContent = "Build with Kairos";
    }
  });
}

export function openBuilderBrief(opener, kind, title, owner) {
  const body = el("div");
  const dialog = openPanelDialog({ title, body, wide: true, group: "tool-store", opener, owner: owner?.firstElementChild });
  renderBuilderBrief(body, kind, dialog);
  return dialog;
}
