// The chat's background in the default appearance (David, 2026-10-07): the
// Kairos figure in halftone while a chat is empty, the sky once it has
// messages. Switching between them dissolves dot by dot: both scenes share
// one dot grid, and the old scene's cells disappear in the dither's own
// order, so the picture re-forms rather than crossfading. Each scene is
// drawn once per size and kept between visits to the tab (drawing the whole
// area takes 100-200 ms).
import { ditherCell, dissolveCells, drawDither, loadDitherImage } from "./dither.js";

const SCENES = {
  figure: { src: "/static/img/home-figure.webp", opts: { cell: 4, focusX: 0.7, focusY: 0.4 } },
  sky: { src: "/static/img/kairos-sky.jpg", opts: { cell: 4 } },
};
const DISSOLVE_MS = 700;
const cache = new Map();  // "scene WxH" -> canvas; the newest few only
const CACHE_LIMIT = 4;

async function sceneCanvas(name, W, H) {
  const key = `${name} ${W}x${H}`;
  if (cache.has(key)) { const hit = cache.get(key); cache.delete(key); cache.set(key, hit); return hit; }
  const scene = SCENES[name];
  const img = await loadDitherImage(scene.src);
  const canvas = document.createElement("canvas");
  const ms = drawDither(canvas, img, W, H, scene.opts);
  canvas.dataset.drawMs = String(Math.round(ms));
  cache.set(key, canvas);
  while (cache.size > CACHE_LIMIT) cache.delete(cache.keys().next().value);
  return canvas;
}

function copy(target, source) {
  target.width = source.width; target.height = source.height;
  target.getContext("2d").drawImage(source, 0, 0);
}

export function mountChatBackdrop(host) {
  const el = document.createElement("div");
  el.className = "chat-backdrop";
  el.setAttribute("aria-hidden", "true");
  const under = document.createElement("canvas");  // the scene being revealed
  const over = document.createElement("canvas");   // the scene on show
  under.className = over.className = "dither-canvas";
  el.append(under, over);
  host.prepend(el);

  let scene = null, size = "", run = 0, disposed = false, resizeTimer = 0;
  let markReady;
  const ready = new Promise((resolve) => { markReady = resolve; });
  const measure = () => {
    const dpr = window.devicePixelRatio || 1;
    return [Math.max(1, Math.round(el.clientWidth * dpr)), Math.max(1, Math.round(el.clientHeight * dpr))];
  };
  const reduced = () => matchMedia("(prefers-reduced-motion: reduce)").matches;

  // Shows `name`; dissolves into it from the current scene when `animate`.
  async function show(name, { animate = false } = {}) {
    if (disposed || (name === scene && size)) return;
    const from = scene;
    scene = name;
    el.dataset.scene = name;
    const token = ++run;
    // Hidden (another appearance has its own background): nothing to draw
    // until it shows, when the resize observer below draws it.
    if (!el.clientWidth) { size = ""; markReady(); return; }
    const [W, H] = measure();
    let target;
    try { target = await sceneCanvas(name, W, H); } catch (_) { markReady(); return; }  // the parchment stays
    if (disposed || token !== run) return;
    size = `${W}x${H}`;
    el.dataset.drawMs = target.dataset.drawMs;  // read by scripts/ui-smoke.cjs
    if (!animate || !from || reduced()) { copy(over, target); markReady(); prepareOther(name, W, H); return; }
    copy(under, target);
    el.classList.add("is-dissolving");
    await dissolveCells(over.getContext("2d"), ditherCell(SCENES[name].opts.cell), DISSOLVE_MS, () => disposed || token !== run);
    el.classList.remove("is-dissolving");
    if (!disposed && token === run) copy(over, target);
  }

  // The other scene is drawn while the app is idle, so the first message's
  // dissolve starts at once instead of after a 100-200 ms draw.
  function prepareOther(name, W, H) {
    const other = name === "figure" ? "sky" : "figure";
    const idle = window.requestIdleCallback || ((fn) => setTimeout(fn, 200));
    idle(() => { if (!disposed && size === `${W}x${H}`) sceneCanvas(other, W, H).catch(() => {}); });
  }

  // A new size redraws the scene on show, without a dissolve. Until then the
  // canvas stretches, which is only seen while dragging the window edge.
  const observer = new ResizeObserver(() => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      const [W, H] = measure();
      if (scene && `${W}x${H}` !== size) { const name = scene; scene = null; show(name); }
    }, 150);
  });
  observer.observe(el);

  return {
    show,
    ready,
    dispose() { disposed = true; clearTimeout(resizeTimer); observer.disconnect(); el.remove(); },
  };
}
