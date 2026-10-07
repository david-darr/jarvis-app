// Desktop-only presentation preference. Mobile always uses the full drawer.
const STORAGE_KEY = "jarvis:sidebar-collapsed";
const root = document.documentElement;
const desktop = window.matchMedia("(min-width: 769px)");

export function restoreSidebar() {
  try { root.classList.toggle("sidebar-collapsed", localStorage.getItem(STORAGE_KEY) === "true"); }
  catch (_) { /* Storage may be unavailable in a private/restricted browser. */ }
}

export function setupSidebar() {
  const sidebar = document.getElementById("sidebar");
  const toggle = document.getElementById("sidebar-toggle");
  toggle.innerHTML = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="3"/><path d="M9 4v16"/><path class="sidebar-toggle-chevron" d="m15 9-3 3 3 3"/></svg>';
  const tooltip = document.createElement("div");
  tooltip.className = "sidebar-tooltip";
  tooltip.setAttribute("aria-hidden", "true"); // The button already has its accessible name.
  document.body.appendChild(tooltip);
  const hide = () => tooltip.classList.remove("show");
  const sync = () => {
    const collapsed = root.classList.contains("sidebar-collapsed");
    toggle.setAttribute("aria-expanded", String(!collapsed));
    toggle.setAttribute("aria-label", collapsed ? "Expand sidebar" : "Collapse sidebar");
    toggle.title = collapsed ? "Expand sidebar" : "Collapse sidebar";
    hide();
  };
  toggle.addEventListener("click", () => {
    if (!desktop.matches) return;
    const collapsed = root.classList.toggle("sidebar-collapsed");
    try { localStorage.setItem(STORAGE_KEY, String(collapsed)); } catch (_) { /* Still works for this visit. */ }
    sync();
  });
  const show = (event) => {
    const button = event.target.closest("button[aria-label]");
    if (!button || !sidebar.contains(button) || !desktop.matches || !root.classList.contains("sidebar-collapsed")) return;
    const rect = button.getBoundingClientRect();
    tooltip.textContent = button.getAttribute("aria-label");
    tooltip.style.left = sidebar.getBoundingClientRect().right + 10 + "px";
    tooltip.style.top = Math.max(8, Math.min(innerHeight - 40, rect.top + (rect.height - 32) / 2)) + "px";
    tooltip.classList.add("show");
  };
  sidebar.addEventListener("pointerover", show);
  sidebar.addEventListener("pointerout", hide);
  sidebar.addEventListener("focusin", show);
  sidebar.addEventListener("focusout", hide);
  sidebar.addEventListener("scroll", hide, true);
  sidebar.addEventListener("click", hide);
  document.addEventListener("keydown", (event) => { if (event.key === "Escape") hide(); });
  window.addEventListener("resize", hide);
  desktop.addEventListener("change", sync);
  sync();
}
