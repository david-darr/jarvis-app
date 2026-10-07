import { el } from "../api.js";
import { ICONS } from "../icons.js";

// Gallery was removed entirely (David's ask 2026-09-01) and Library shipped
// for real in Phase 7 — no tab currently uses this stub. Left in place
// (empty) as the honest landing spot for any future genuinely-deferred tab,
// same reasoning as app.js's STUB_TABS.
const MESSAGES = {};

export async function render(container, tabId) {
  container.innerHTML = "";
  const icon = el("div", { class: "icon-large", style: "width:48px;height:48px;color:var(--text-faint);" });
  icon.innerHTML = ICONS[tabId] || "";
  container.appendChild(
    el("div", { class: "coming-soon" }, [
      icon,
      el("h3", { text: "Coming later" }),
      el("p", { text: MESSAGES[tabId] || "Not built yet." }),
    ])
  );
}
