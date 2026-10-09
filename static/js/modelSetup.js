// One guide for onboarding, Settings and an empty chat.
import { api, el, openPanelDialog } from "./api.js";
import { showPermissionPrompt, dismissPermissionPrompt } from "./permissionPrompt.js";

const post = (path, body, signal) => api(`/api/model-setup${path}`, {
  method: "POST", body: body ? JSON.stringify(body) : undefined, signal,
});
const WORDS = [
  ["CLI", "A small app that runs in a text window. Kairos uses it for you after you sign in."],
  ["Agent and harness", "An agent can use tools to help with a task. A harness is the app around the model that gives it those tools and keeps track of its work."],
  ["API key", "A private code that lets an app use a model on your behalf. Keep it secret, just like a password."],
  ["Subscription and pay-per-use", "A subscription is a regular payment with usage limits. An API key usually charges for what you use, separately from your Claude or ChatGPT subscription."],
  ["Local model", "A model downloaded onto this computer. It works without an account and keeps the conversation here, but may be slower or less capable."],
];

export function openModelSetup({ onConnected } = {}) {
  const body = el("div");
  let dispose;
  const dialog = openPanelDialog({ title: "Choose how Kairos thinks", body, wide: true,
    group: "model-setup", className: "model-setup-panel", onClose: () => dispose?.() });
  dispose = mountModelSetup(body, { onConnected });
  return dialog;
}

export function mountModelSetup(host, { onConnected } = {}) {
  const controller = new AbortController();
  const { signal } = controller;
  let state = null, generation = 0, timer = null, disposed = false, jobId = null, localDispose = null;
  const root = el("div", { class: "model-setup", "data-model-setup": "guide" });
  const screen = el("div", { class: "model-setup-screen", "aria-live": "polite" });
  const words = el("details", { class: "model-setup-words" }, [
    el("summary", { text: "What do these words mean?" }),
    ...WORDS.map(([word, meaning]) => el("div", {}, [el("h4", { text: word }), el("p", { text: meaning })])),
  ]);
  root.append(el("p", { class: "sub", text: "Choose the option that fits you. Kairos will help you connect it, and you can change it any time." }), screen, words);
  host.replaceChildren(root);
  const button = (text, action, attrs = {}) => el("button", { type: "button", class: "btn", text, ...attrs,
    onclick: async event => {
      const target = event.currentTarget;
      target.disabled = true;
      try { await action(); } catch (error) { if (!disposed && error.name !== "AbortError") problem(error.message); }
      finally { if (target.isConnected) target.disabled = false; }
    } });
  function stop() {
    generation++;
    clearTimeout(timer);
    localDispose?.(); localDispose = null;
    dismissPermissionPrompt();
    if (jobId) { post(`/codex/install/${jobId}/cancel`).catch(() => {}); jobId = null; }
  }
  function problem(text) {
    screen.querySelector(".model-setup-error")?.remove();
    screen.append(el("p", { class: "model-setup-error", role: "alert", text }));
  }
  function frame(title, copy, ...children) {
    screen.replaceChildren(el("h3", { text: title }), el("p", { text: copy }), ...children,
      button("Back to choices", choices, { class: "btn quiet" }));
  }
  async function refresh() { state = await api("/api/model-setup/status", { signal }); return state; }
  async function choices() {
    stop(); jobId = null;
    screen.replaceChildren(el("p", { text: "Checking this computer..." }));
    const at = generation;
    await refresh();
    if (disposed || at !== generation) return;
    const grid = el("div", { class: "model-setup-cards" });
    const has = kind => !!state.connections?.[kind]?.length;
    for (const [kind, title, copy, ready, label] of [
      ["claude", "I pay for Claude", "For Claude Pro or Max. Claude Code is already inside Kairos, so you only need to sign in. Your subscription's limits apply.", has("claude_cli") && state.claude.signed_in, state.claude.signed_in ? "Signed in on this computer" : "No download needed"],
      ["codex", "I pay for ChatGPT", "For ChatGPT Plus or Pro. Codex is OpenAI's agent app. Kairos can install it, then help you sign in. Your subscription's limits apply.", has("codex_cli") && state.codex.signed_in, state.codex.installed ? `Installed${state.codex.version ? ` (${state.codex.version})` : ""}` : "Needs a one-time install"],
      ["api", "I have an API key", "Choose OpenAI, Anthropic, OpenRouter or Google. You pay the provider for what you use. Kairos tests and saves your key securely.", has("api"), "Pay for what you use"],
      ["local", "Free, on this computer", "Download a small model through Cookbook. No account or usage bill. Private, but slower and less capable than a cloud model.", has("local"), "Needs disk space"],
    ]) grid.append(el("section", { class: "model-setup-choice", "data-setup-kind": kind }, [
      el("h3", { text: title }), el("p", { text: copy }), el("p", { class: "meta model-setup-card-status", text: ready ? "Already set up" : label }),
      button(ready ? "Review setup" : "Choose this", () => choose(kind)),
    ]));
    screen.replaceChildren(grid);
    async function pollCards() {
      if (disposed || at !== generation || !root.isConnected) return;
      try {
        await refresh();
        if (disposed || at !== generation) return;
        for (const card of grid.children) {
          const kind = card.dataset.setupKind;
          const ready = has(kind === 'claude' || kind === 'codex' ? kind + '_cli' : kind) &&
            (kind === 'claude' ? state.claude.available && state.claude.signed_in : kind === 'codex' ? state.codex.installed && state.codex.signed_in : true);
          const label = kind === 'claude' ? state.claude.signed_in ? 'Signed in on this computer' : 'No download needed' :
            kind === 'codex' ? state.codex.installed ? `Installed${state.codex.version ? ` (${state.codex.version})` : ''}` : 'Needs a one-time install' :
              kind === 'api' ? 'Pay for what you use' : 'Needs disk space';
          card.querySelector('.model-setup-card-status').textContent = ready ? 'Already set up' : label;
          card.querySelector('button').textContent = ready ? 'Review setup' : 'Choose this';
        }
      } catch { /* Keep the last successful check during a brief outage. */ }
      if (!disposed && at === generation) timer = setTimeout(pollCards, 4000);
    }
    timer = setTimeout(pollCards, 4000);
  }
  async function connected(kind) {
    const at = generation;
    const result = await post("/connections", { kind }, signal);
    if (disposed || at !== generation) return;
    const account = state[kind]?.account;
    frame("Connected", `${account ? `Connected as ${account}` : `${result.connection.name} is connected`}. Pick it from the model menu above the chat box when you start a conversation.`,
      button("Done", choices));
    document.dispatchEvent(new CustomEvent("kairos:models-changed", { detail: result }));
    await onConnected?.(result);
  }
  async function signIn(kind) {
    stop();
    const at = generation;
    await post(`/sign-in/${kind}`, null, signal);
    if (disposed || at !== generation) return;
    const cancel = button("Cancel waiting", choices);
    frame("Waiting for you to finish signing in", "Finish in the vendor's window. Kairos checks that a login exists; your password stays with the provider.", cancel);
    const deadline = Date.now() + 5 * 60 * 1000;
    async function poll() {
      if (disposed || at !== generation || !root.isConnected) return;
      try {
        const result = await api(`/api/model-setup/sign-in/${kind}/wait?timeout=0`, { signal });
        if (disposed || at !== generation) return;
        if (result.signed_in) { await connected(kind); return; }
        if (Date.now() >= deadline) { frame("Sign-in is taking a while", "You can try again when you are ready.", button("Try sign-in again", () => signIn(kind))); return; }
        timer = setTimeout(poll, 1500);
      } catch (error) { if (!disposed && at === generation) problem(error.message); }
    }
    timer = setTimeout(poll, 1500);
  }
  function cli(kind) {
    const current = state[kind];
    const title = kind === "claude" ? "Sign in with Claude" : "Sign in with ChatGPT";
    if (kind === "claude" && !current.available) {
      frame("Claude Code could not be found", "This copy of Kairos could not find its bundled Claude app. Repair or update Kairos, then try again."); return;
    }
    if (kind === "codex" && !current.installed) {
      frame("ChatGPT with Codex", "Codex is OpenAI's agent app. It lets Kairos work with your ChatGPT subscription. Installation asks for your permission and shows the exact command or download URL.", button("Install Codex", install)); return;
    }
    frame(title, current.signed_in ? "You are already signed in on this computer. Connect it to Kairos." : "A text window will open with the provider's own sign-in. Follow its steps, then come back here.",
      button(current.signed_in ? "Connect to Kairos" : title, () => current.signed_in ? connected(kind) : signIn(kind)));
  }
  async function install() {
    stop(); const at = generation;
    const job = await post("/codex/install", null, signal); jobId = job.id;
    if (disposed || at !== generation) { post(`/codex/install/${job.id}/cancel`).catch(() => {}); jobId = null; return; }
    const logs = el("pre", { class: "model-setup-progress", text: "Waiting for permission..." });
    const approvalHost = el("div");
    frame("Installing Codex", "Kairos will show the steps here. You can keep this guide open while it installs.", logs, approvalHost,
      button("Cancel approval", async () => { await post(`/codex/install/${jobId}/cancel`, null, signal); dismissPermissionPrompt(); await choices(); }));
    let lastPrompt = null;
    async function poll() {
      if (disposed || at !== generation || !root.isConnected) return;
      try {
        const progress = await api(`/api/model-setup/codex/install/${job.id}`, { signal });
        if (disposed || at !== generation) return;
        logs.textContent = progress.lines.join("\n") || "Waiting for permission...";
        if (progress.permission && progress.permission.id !== lastPrompt) {
          lastPrompt = progress.permission.id;
          showPermissionPrompt({ ...progress.permission, tool: "Install Codex" }, () => {}, { mount: approvalHost });
        }
        // A finished job has nothing left to cancel: forget it, or Try again's stop() cancels it late.
        if (progress.state === "error") { jobId = null; dismissPermissionPrompt(); problem(progress.error); screen.append(button("Try again", install)); return; }
        if (progress.state === "done") {
          dismissPermissionPrompt(); jobId = null; await refresh();
          if (!disposed && at === generation) cli("codex");
          return;
        }
        timer = setTimeout(poll, 700);
      } catch (error) { if (!disposed && at === generation) problem(error.message); }
    }
    await poll();
  }
  function apiKey() {
    const provider = el("select", { "aria-label": "API provider" }, [
      ...[["openai", "OpenAI"], ["anthropic", "Anthropic"], ["openrouter", "OpenRouter"], ["google", "Google"]].map(([value, text]) => el("option", { value, text })),
    ]);
    frame("Choose your API provider", "API use is billed separately from a Claude or ChatGPT subscription.",
      provider, button("Continue", () => {
        const selected = provider.value;
        const key = el("input", { type: "password", autocomplete: "off", "aria-label": "API key", placeholder: "Paste your API key" });
        frame("Test your API key", "Kairos sends one tiny test, then saves a working key encrypted on this computer. The test may cost a small amount.", key,
          button("Test and save", async () => {
            const at = generation;
            if (!key.value.trim()) { problem("Paste an API key first."); return; }
            const result = await post("/connections", { kind: "api", provider: selected, key: key.value.trim() }, signal);
            key.value = "";
            if (disposed || at !== generation) return;
            frame("API key saved", `${result.connection.name} is connected. Pick it from the model menu above the chat box when you start a conversation.`, button("Done", choices));
            document.dispatchEvent(new CustomEvent("kairos:models-changed", { detail: result })); await onConnected?.(result);
          }));
        key.focus();
      }));
  }
  async function choose(kind) {
    stop();
    if (kind === "api") { apiKey(); return; }
    if (kind === "local") {
      const local = el("div");
      frame("Cookbook's small model", "Cookbook will download and prepare a model on this computer. You can return to setup while it works.", local);
      const at = generation;
      const cookbook = await import("./views/cookbook.js");
      if (disposed || at !== generation) return;
      const dispose = await cookbook.mountModelRecommendation(local, { onReturn: choices, signal });
      if (disposed || at !== generation) dispose(); else localDispose = dispose;
      return;
    }
    cli(kind);
  }
  choices().catch(error => { if (!disposed) problem(error.message); });
  return () => {
    disposed = true; stop(); controller.abort(); dismissPermissionPrompt();
  };
}
