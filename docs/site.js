// Kairos site behaviour: the hero halftone, the navigation, the preview
// switcher, and download buttons that match the visitor's system and the
// latest release. Everything degrades to plain links when scripts or the
// network are unavailable.
const REPO = "david-darr/kairos";

// -- hero halftone (dither.js) --------------------------------------------
const art = document.querySelector(".hero-art");
if (art && window.kairosDither) {
  window.kairosDither(art, "img/hero-figure.webp", { cell: 4, fade: [0.72, 1.0], focusX: 0.62, focusY: 0.35 });
}

// -- navigation ----------------------------------------------------------
const nav = document.querySelector(".site-nav");
const menu = document.querySelector(".menu-button");
const onScroll = () => nav.classList.toggle("scrolled", window.scrollY > 8);
onScroll();
window.addEventListener("scroll", onScroll, { passive: true });
function setMenu(open) {
  nav.classList.toggle("open", open);
  menu.setAttribute("aria-expanded", String(open));
}
menu.addEventListener("click", () => setMenu(!nav.classList.contains("open")));
document.querySelectorAll(".nav-links a").forEach((link) => link.addEventListener("click", () => setMenu(false)));
document.addEventListener("keydown", (event) => { if (event.key === "Escape" && nav.classList.contains("open")) { setMenu(false); menu.focus(); } });

// -- preview switcher ----------------------------------------------------
const previews = {
  home: ["The opening is now.", "Kairos Home with a quiet sky card, recent conversations and upcoming work. Demo data."],
  "chat-new": ["One thought is enough to begin.", "A centered new-chat composer, hidden chat history and a slim icon rail. Demo data."],
  chat: ["A little space to think.", "A focused conversation with its floating composer and model controls. Demo data."],
  vault: ["A connected home for your knowledge.", "The vault map with linked notes and folders. Demo data."],
  "sidebar-collapsed": ["More room. Everything still in reach.", "Kairos Home with its sidebar collapsed to an icon rail. Demo data."],
};
const previewImage = document.getElementById("preview-image");
let selection = 0;
document.querySelectorAll("[data-preview]").forEach((button) => {
  button.addEventListener("click", async () => {
    const key = button.dataset.preview;
    const version = ++selection;
    const source = "img/" + key + ".png";
    const image = new Image();
    image.src = source;
    try { await image.decode(); } catch (_) { return; }
    if (version !== selection) return;
    document.querySelectorAll("[data-preview]").forEach((item) => {
      item.classList.toggle("active", item === button);
      item.setAttribute("aria-pressed", String(item === button));
    });
    previewImage.classList.remove("changing");
    previewImage.src = source;
    previewImage.alt = previews[key][1];
    document.getElementById("preview-link").href = source;
    document.getElementById("preview-caption").textContent = previews[key][0];
    requestAnimationFrame(() => previewImage.classList.add("changing"));
  });
});

// -- downloads: the visitor's system, then the latest release's files ---------
function platform() {
  const hint = (navigator.userAgentData && navigator.userAgentData.platform) || navigator.platform || navigator.userAgent || "";
  if (/win/i.test(hint)) return "windows";
  if (/mac/i.test(hint) && !/iphone|ipad/i.test(navigator.userAgent)) return "mac";
  return null;
}
const system = platform();
const primary = document.getElementById("primary-download");
if (system) {
  primary.firstChild.textContent = system === "windows" ? "Download for Windows " : "Download for macOS ";
  const card = document.querySelector(`.download-card[data-platform="${system}"]`);
  if (card) card.classList.add("recommended");
}

fetch(`https://api.github.com/repos/${REPO}/releases/latest`, { headers: { Accept: "application/vnd.github+json" } })
  .then((response) => (response.ok ? response.json() : Promise.reject(response.status)))
  .then((release) => {
    const find = (ext) => (release.assets || []).find((asset) => asset.name.toLowerCase().endsWith(ext));
    document.querySelectorAll("[data-asset]").forEach((link) => {
      const asset = find(link.dataset.asset);
      if (asset) link.href = asset.browser_download_url;
    });
    document.querySelectorAll("[data-file]").forEach((label) => {
      const asset = find(label.dataset.file);
      if (asset) label.textContent = `${asset.name} · ${(asset.size / 1048576).toFixed(0)} MB`;
    });
    const mine = system && find(system === "windows" ? ".exe" : ".dmg");
    if (mine) primary.href = mine.browser_download_url;
    const version = String(release.tag_name || "").replace(/^v/, "");
    if (version) document.getElementById("release-line").textContent = `Version ${version} · Windows 10 and 11 · macOS on Apple Silicon · MIT license`;
  })
  .catch(() => { /* the links already point at the latest release page */ });
