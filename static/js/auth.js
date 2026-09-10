import { api, el } from "./api.js";

// Real gap found live, 2026-08-31: AUTH_ENABLED=true (the Tailscale remote
// listener - see scripts/run_remote.py) left the whole app blank on first
// load. Root cause: app.js's boot() called /api/settings (admin-gated)
// before any login existed, so the fetch 401'd, the promise rejected
// unhandled, and nothing ever rendered - no visible error either, since it
// never reached a place that could show one. Phase 1's auth backend
// (core/auth.py, routes/auth_routes.py) was real and fully tested, but only
// ever over curl/API calls with AUTH_ENABLED=false locally - this is the
// frontend piece that was simply never built. Reuses the same
// .onboarding-overlay/.onboarding-card styling as onboarding.js so it
// doesn't need its own CSS.
//
// run() resolves once a valid session exists (or immediately if
// AUTH_ENABLED=false) - callers should await it before touching any
// authenticated API.
export async function run(overlay) {
  const status = await api("/api/auth/status");
  if (!status.auth_enabled || status.username) return;

  await new Promise((resolve) => {
    if (status.setup_required) renderSetup(overlay, resolve);
    else renderLogin(overlay, resolve);
  });
}

function backLink(text, onclick) {
  return el("div", { class: "sub", style: "margin-top:10px;text-align:center;cursor:pointer;text-decoration:underline;", text, onclick });
}

function card(children) {
  return el("div", { class: "glass bracket card onboarding-card" }, children);
}

function renderSetup(overlay, onDone) {
  const userInput = el("input", { placeholder: "Username", autocomplete: "username" });
  const passInput = el("input", { type: "password", placeholder: "Password", autocomplete: "new-password" });
  const err = el("div", { class: "meta", style: "color: var(--danger, #e55); min-height: 18px; margin-top: 8px;" });

  const submit = async () => {
    err.textContent = "";
    try {
      await api("/api/auth/setup", {
        method: "POST",
        body: JSON.stringify({ username: userInput.value.trim(), password: passInput.value }),
      });
      onDone();
    } catch (e) {
      err.textContent = e.message.replace(/^\d+: /, "");
    }
  };
  passInput.addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); });

  overlay.innerHTML = "";
  overlay.appendChild(card([
    el("h2", { text: "Set up JARVIS" }),
    el("div", { class: "sub", text: "First connection to this instance — create the admin account. This is your login, not a chat setting." }),
    userInput,
    passInput,
    err,
    el("div", { class: "onboarding-actions" }, [
      el("div", {}),
      el("button", { class: "btn", text: "Create Account", onclick: submit }),
    ]),
  ]));
  userInput.focus();
}

function renderLogin(overlay, onDone) {
  const userInput = el("input", { placeholder: "Username", autocomplete: "username" });
  const passInput = el("input", { type: "password", placeholder: "Password", autocomplete: "current-password" });
  const totpInput = el("input", { placeholder: "2FA code", style: "display:none;" });
  const err = el("div", { class: "meta", style: "color: var(--danger, #e55); min-height: 18px; margin-top: 8px;" });

  const submit = async () => {
    err.textContent = "";
    try {
      await api("/api/auth/login", {
        method: "POST",
        body: JSON.stringify({
          username: userInput.value.trim(),
          password: passInput.value,
          totp_code: totpInput.value.trim() || null,
        }),
      });
      onDone();
    } catch (e) {
      const msg = e.message.replace(/^\d+: /, "");
      if (msg.includes("2FA")) totpInput.style.display = "";
      err.textContent = msg;
    }
  };
  totpInput.addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); });
  passInput.addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); });

  overlay.innerHTML = "";
  overlay.appendChild(card([
    el("h2", { text: "JARVIS" }),
    el("div", { class: "sub", text: "Sign in to continue." }),
    userInput,
    passInput,
    totpInput,
    err,
    el("div", { class: "onboarding-actions" }, [
      el("div", {}),
      el("button", { class: "btn", text: "Log In", onclick: submit }),
    ]),
    backLink("Forgot password?", () => renderForgotPassword(overlay, onDone)),
  ]));
  userInput.focus();
}

// Forgot password (David's ask 2026-09-10): there's no per-user email field
// anywhere in auth.json, so identity is checked against whatever's already
// configured in the Email tab instead - enter a username plus an address
// that matches one of those accounts, and a 6-digit code goes out through
// that same account's own SMTP. Two steps, same shape as TOTP's
// enroll-then-confirm flow: request the code, then redeem it.
function renderForgotPassword(overlay, onDone) {
  const userInput = el("input", { placeholder: "Username", autocomplete: "username" });
  const emailInput = el("input", { placeholder: "Connected email address", type: "email", autocomplete: "email" });
  const err = el("div", { class: "meta", style: "color: var(--danger, #e55); min-height: 18px; margin-top: 8px;" });
  const info = el("div", { class: "meta", style: "min-height: 18px; margin-top: 8px;" });

  const submit = async () => {
    err.textContent = ""; info.textContent = "";
    if (!userInput.value.trim() || !emailInput.value.trim()) {
      err.textContent = "Enter both your username and the connected email.";
      return;
    }
    try {
      const r = await api("/api/auth/forgot-password", {
        method: "POST",
        body: JSON.stringify({ username: userInput.value.trim(), email: emailInput.value.trim() }),
      });
      // Deliberately the same message whether or not anything actually
      // matched server-side - the backend already made that choice
      // (routes/auth_routes.py), the UI just displays it as-is rather than
      // inferring anything from it.
      info.textContent = r.message;
      renderResetCode(overlay, userInput.value.trim(), onDone);
    } catch (e) {
      err.textContent = e.message.replace(/^\d+: /, "");
    }
  };
  emailInput.addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); });

  overlay.innerHTML = "";
  overlay.appendChild(card([
    el("h2", { text: "Reset password" }),
    el("div", { class: "sub", text: "Enter your username and the email address already connected in the Email tab. If they match, a code goes out to that address." }),
    userInput,
    emailInput,
    err,
    info,
    el("div", { class: "onboarding-actions" }, [
      el("div", {}),
      el("button", { class: "btn", text: "Send code", onclick: submit }),
    ]),
    backLink("Back to sign in", () => renderLogin(overlay, onDone)),
  ]));
  userInput.focus();
}

function renderResetCode(overlay, username, onDone) {
  const codeInput = el("input", { placeholder: "6-digit code", autocomplete: "one-time-code", inputmode: "numeric" });
  const passInput = el("input", { type: "password", placeholder: "New password (8+ characters)", autocomplete: "new-password" });
  const err = el("div", { class: "meta", style: "color: var(--danger, #e55); min-height: 18px; margin-top: 8px;" });

  const submit = async () => {
    err.textContent = "";
    try {
      await api("/api/auth/reset-password", {
        method: "POST",
        body: JSON.stringify({ username, code: codeInput.value.trim(), new_password: passInput.value }),
      });
      renderLogin(overlay, onDone);
    } catch (e) {
      err.textContent = e.message.replace(/^\d+: /, "");
    }
  };
  passInput.addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); });

  overlay.innerHTML = "";
  overlay.appendChild(card([
    el("h2", { text: "Enter your code" }),
    el("div", { class: "sub", text: `Check the inbox for the email you entered. The code expires in 15 minutes.` }),
    codeInput,
    passInput,
    err,
    el("div", { class: "onboarding-actions" }, [
      el("div", {}),
      el("button", { class: "btn", text: "Reset password", onclick: submit }),
    ]),
    backLink("Start over", () => renderForgotPassword(overlay, onDone)),
  ]));
  codeInput.focus();
}
