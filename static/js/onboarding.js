import { api, el } from "./api.js";

// First-run wizard (David's ask, 2026-08-31) — shown once, in place of the
// normal sidebar+view shell, until settings.onboarding_complete is true.
// Reuses the same endpoints Settings/Cookbook already expose (vault-dir,
// Discord config) rather than any special onboarding-only API surface, so
// "set it up now" and "set it up later from Settings" are the exact same
// code path.
const STEPS = ["welcome", "vault", "discord", "remote", "finish"];

export async function run(overlay, onComplete) {
  let step = 0;
  let settings = await api("/api/settings");

  function renderStep() {
    overlay.innerHTML = "";
    const card = el("div", { class: "glass bracket card onboarding-card" });
    const dots = el("div", { class: "onboarding-steps" });
    STEPS.forEach((_, i) => dots.appendChild(el("div", { class: "dot" + (i === step ? " active" : "") })));
    card.appendChild(dots);
    card.appendChild(STEP_RENDERERS[STEPS[step]](card));
    overlay.appendChild(card);
  }

  function goTo(i) { step = i; renderStep(); }

  const STEP_RENDERERS = {
    welcome: () => el("div", {}, [
      el("h2", { text: "All systems online." }),
      el("div", { class: "sub", text: "Let's get JARVIS set up on this machine. This takes under a minute — everything here can also be changed later from Settings or Cookbook." }),
      el("div", { class: "onboarding-actions" }, [
        el("div", {}),
        el("button", { class: "btn", text: "Get Started", onclick: () => goTo(1) }),
      ]),
    ]),

    vault: () => {
      const pathEl = el("div", { class: "meta", style: "margin:10px 0;", text: settings.vault_dir });
      const pickBtn = el("button", { class: "btn", text: "Choose Existing Vault..." });
      if (!window.jarvis) {
        pickBtn.disabled = true;
        pickBtn.title = "Folder picking is only available in the desktop app";
      }
      pickBtn.addEventListener("click", async () => {
        const picked = await window.jarvis.pickVaultFolder();
        if (!picked) return;
        await api("/api/settings/vault-dir", { method: "POST", body: JSON.stringify({ path: picked }) });
        settings = await api("/api/settings");
        pathEl.textContent = settings.vault_dir;
        questionnaireWrap.style.display = "none";
      });

      // Questionnaire (David's ask 2026-08-31) — only relevant if the user
      // doesn't already have a vault to point at; builds folder structure +
      // a personalized index instead of the generic scaffold. Hidden until
      // "Build My Vault" is clicked so the default path (skip straight to
      // Discord) stays a single click, same as before this change.
      const AREAS = [
        ["work", "Work & Projects"],
        ["health", "Health & Fitness"],
        ["finances", "Finances"],
        ["learning", "Learning"],
        ["home", "Home & Family"],
      ];
      const checkboxes = AREAS.map(([key, label]) => {
        const cb = el("input", { type: "checkbox", value: key });
        return { key, cb, row: el("label", { style: "display:flex;align-items:center;gap:8px;margin:4px 0;" }, [cb, label]) };
      });
      const profileInput = el("textarea", {
        rows: "3",
        style: "width:100%;margin-top:10px;",
        placeholder: "Tell JARVIS a bit about yourself — your role, what you're working on. It'll write this straight into the vault index instead of leaving it blank.",
      });
      const buildStatus = el("div", { class: "meta", style: "margin-top:8px;" });
      const questionnaireWrap = el("div", { style: "display:none;margin-top:12px;" }, [
        el("div", { class: "sub", text: "What do you want JARVIS tracking for you? (Tasks and daily notes are included either way.)" }),
        ...checkboxes.map((c) => c.row),
        profileInput,
        el("button", { class: "btn", style: "margin-top:10px;", text: "Build My Vault", onclick: async () => {
          const areas = checkboxes.filter((c) => c.cb.checked).map((c) => c.key);
          const res = await api("/api/settings/vault-setup", {
            method: "POST",
            body: JSON.stringify({ areas, profile_note: profileInput.value.trim() }),
          });
          buildStatus.textContent = res.applied
            ? "Vault built."
            : "This vault already has real content, so it was left untouched.";
        }}),
        buildStatus,
      ]);
      const questionnaireBtn = el("button", { class: "btn", text: "Build My Vault (questionnaire)", onclick: () => {
        questionnaireWrap.style.display = questionnaireWrap.style.display === "none" ? "block" : "none";
      }});

      return el("div", {}, [
        el("h2", { text: "Your vault" }),
        el("div", { class: "sub", text: "This is where JARVIS's memory lives — notes, priorities, everything it remembers between sessions. Use the seeded default, point it at a vault you already have, or answer a few quick questions and JARVIS will build one shaped to you." }),
        pathEl,
        el("div", { style: "display:flex;gap:8px;" }, [pickBtn, questionnaireBtn]),
        questionnaireWrap,
        el("div", { class: "onboarding-actions" }, [
          el("button", { class: "btn", text: "Back", onclick: () => goTo(0) }),
          el("button", { class: "btn", text: "Continue", onclick: () => goTo(2) }),
        ]),
      ]);
    },

    discord: () => {
      const tokenInput = el("input", { type: "password", placeholder: "Discord bot token", style: "width:100%;margin:10px 0 8px;" });
      const allowedInput = el("input", { placeholder: "Your Discord user ID (optional, restricts who it replies to)", style: "width:100%;" });
      const statusEl = el("div", { class: "meta", style: "margin-top:8px;" });
      const connectBtn = el("button", { class: "btn", text: "Connect", onclick: async () => {
        if (!tokenInput.value.trim()) return;
        // Named generically here — multiple named bots (David's ask
        // 2026-09-01) and picking this one's default model both happen in
        // Settings > Channels afterward, not during this quick-setup step.
        await api("/api/settings/discord-bots", {
          method: "POST",
          body: JSON.stringify({ name: "Discord Bot", token: tokenInput.value.trim(), allowed_user_id: allowedInput.value.trim() || null }),
        });
        statusEl.textContent = "Connected. Pick its default model later in Settings > Channels.";
      }});
      return el("div", {}, [
        el("h2", { text: "Talk to JARVIS from Discord" }),
        el("div", { class: "sub", text: "Optional — Discord is the fastest way to reach JARVIS outside this app. Paste a bot token now, or skip and add it later from Settings." }),
        tokenInput, allowedInput, connectBtn, statusEl,
        el("div", { class: "onboarding-actions" }, [
          el("button", { class: "btn", text: "Back", onclick: () => goTo(1) }),
          el("button", { class: "btn", text: "Skip / Continue", onclick: () => goTo(3) }),
        ]),
      ]);
    },

    // Remote access (David's ask 2026-09-03). Deliberately introduces the
    // feature and checks what's already in place, but doesn't try to run the
    // whole multi-step setup inside the wizard — installing and signing into
    // Tailscale happens outside this app, so pushing someone through it here
    // would mean a dead-end step for anyone who doesn't have it yet. The
    // full guided checklist lives in Settings > Remote Access, which this
    // links to; onboarding's job is making sure users know it exists.
    remote: () => {
      const statusEl = el("div", { class: "meta", style: "margin:10px 0;line-height:1.6;", text: "Checking this machine…" });

      api("/api/remote/status")
        .then((s) => {
          if (s.running_now && s.url) {
            statusEl.textContent = `Already on — reachable at ${s.url}`;
          } else if (s.installed && s.logged_in) {
            statusEl.textContent = `Tailscale is installed and signed in on this machine (${s.hostname || "ready"}). You can switch remote access on in Settings → Remote Access whenever you want it.`;
          } else if (s.installed) {
            statusEl.textContent = "Tailscale is installed here but not signed in yet. Sign in, then finish setup in Settings → Remote Access.";
          } else {
            statusEl.textContent = "Tailscale isn't installed on this machine yet. It's free, and it's what makes the connection private — install it, then finish setup in Settings → Remote Access.";
          }
        })
        .catch(() => {
          statusEl.textContent = "You can set this up any time from Settings → Remote Access.";
        });

      return el("div", {}, [
        el("h2", { text: "Reach JARVIS from anywhere" }),
        el("div", { class: "sub", text: "Optional — JARVIS can be reachable from your phone or another computer over Tailscale, a private network between your own devices. Nothing is exposed to the public internet, and it requires a login." }),
        statusEl,
        el("a", {
          href: "https://tailscale.com/download", target: "_blank", rel: "noopener",
          class: "btn", style: "text-decoration:none;display:inline-block;",
          text: "Get Tailscale",
        }),
        el("div", { class: "onboarding-actions" }, [
          el("button", { class: "btn", text: "Back", onclick: () => goTo(2) }),
          el("button", { class: "btn", text: "Skip / Continue", onclick: () => goTo(4) }),
        ]),
      ]);
    },

    finish: () => el("div", {}, [
      el("h2", { text: "You're set." }),
      el("div", { class: "sub", text: "One last thing — JARVIS doesn't come with a default model. Head to Settings → Add Models to connect Claude Code CLI, a local server (Ollama, llama.cpp, vLLM), or an API provider, then pick it from the dropdown above the chat box. You can add more any time." }),
      el("div", { class: "onboarding-actions" }, [
        el("button", { class: "btn", text: "Back", onclick: () => goTo(3) }),
        el("button", { class: "btn", text: "Enter JARVIS", onclick: async () => {
          await api("/api/settings/onboarding-complete", { method: "POST" });
          onComplete();
        }}),
      ]),
    ]),
  };

  renderStep();
}
