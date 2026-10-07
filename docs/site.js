// Kairos site behaviour: the hero halftone, the navigation, and download
// buttons that match the visitor's system and the latest release. Everything
// degrades to plain links when scripts or the network are unavailable. The
// live demo (docs/demo) runs on its own in its frame.
// The halftone renderer is the app's own (static/js/dither.js), from the demo's copy.
import { mountDither } from "./demo/static/js/dither.js";
const REPO = "david-darr/kairos";

// -- hero halftone ----------------------------------------------------------
const art = document.querySelector(".hero-art");
if (art) mountDither(art, "img/hero-figure.webp", { cell: 4, fade: [0.72, 1.0], focusX: 0.5, focusY: 0.35 });

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
