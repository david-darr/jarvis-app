// The per-launch secret that makes the backend answer only JARVIS's own
// windows when accounts are off (see core/auth.py's UI_SECRET). Before this,
// every local request counted as the one admin user, so any program on the
// machine - an agent's shell included - could export the backup or wipe data
// through the API.
//
// main.js creates the secret, passes it to the backend it starts, and sets it
// as a cookie in the app's own session: the main window and the usage overlay
// share that session; the side browser deliberately does not (browser.js), so
// a web page never carries it.

const crypto = require("crypto");

const UI_COOKIE_NAME = "jarvis_ui";

function createUiSecret() {
  return crypto.randomBytes(32).toString("hex");
}

// A session cookie (no expiry, so never written to disk), unreadable by page
// scripts, and never sent on a request another site starts.
function uiCookie(backendUrl, secret) {
  return { url: backendUrl, name: UI_COOKIE_NAME, value: secret, httpOnly: true, sameSite: "strict" };
}

// "Open in browser": the person's own browser has no cookie, so ask the
// backend for a one-time code (only a holder of the secret can) and open the
// link that trades it for the cookie.
async function browserHandoffUrl(fetchImpl, backendUrl, secret) {
  const res = await fetchImpl(`${backendUrl}/api/auth/ui-code`, {
    method: "POST",
    headers: { Cookie: `${UI_COOKIE_NAME}=${secret}` },
  });
  if (!res.ok) throw new Error(`the backend refused a browser code (${res.status})`);
  const { code } = await res.json();
  return `${backendUrl}/api/auth/ui-handoff?code=${encodeURIComponent(code)}`;
}

module.exports = { UI_COOKIE_NAME, createUiSecret, uiCookie, browserHandoffUrl };
