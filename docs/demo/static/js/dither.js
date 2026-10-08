// Kairos halftone (brand handoff, BRAND.md 6.8; adapted from its
// code/ditherBackground.js). Draws an image as round dots on a 4x4 ordered
// dither of brightness, at the element's real device-pixel size, so the dot
// grid is never resampled (a scaled dot image shows moire). Used by Home's
// banner, the chat background (chatBackdrop.js) and the website's hero
// (docs/, through the demo copy). Whatever the element shows underneath (a
// plain image) stays if drawing can't happen.
export const BAYER4 = [0, 8, 2, 10, 12, 4, 14, 6, 3, 11, 1, 9, 15, 7, 13, 5].map((v) => v / 16 + 1 / 32);

// The dot cell in device pixels, for a given cell size in CSS pixels.
export function ditherCell(cell = 4) {
  return Math.max(2, Math.round(cell * (window.devicePixelRatio || 1)));
}

// Clears `ctx`'s canvas cell by cell in the dither's own order over `ms`,
// revealing whatever is underneath: the chat background's dissolve
// (chatBackdrop.js) and the website hero developing in. Resolves when done,
// or early when `cancelled()` turns true.
const ORDER = BAYER4.map((t, i) => [t, i % 4, Math.floor(i / 4)]).sort((a, b) => a[0] - b[0]);
export function dissolveCells(ctx, c, ms, cancelled = () => false) {
  const cols = Math.ceil(ctx.canvas.width / c), rows = Math.ceil(ctx.canvas.height / c);
  const started = performance.now();
  let cleared = 0;
  return new Promise((resolve) => {
    const step = (now) => {
      if (cancelled()) { resolve(); return; }
      const due = Math.min(ORDER.length, Math.ceil(((now - started) / ms) * ORDER.length));
      for (; cleared < due; cleared++) {
        const [, bx, by] = ORDER[cleared];
        for (let y = by; y < rows; y += 4) for (let x = bx; x < cols; x += 4) ctx.clearRect(x * c, y * c, c, c);
      }
      if (cleared < ORDER.length) requestAnimationFrame(step); else resolve();
    };
    requestAnimationFrame(step);
  });
}

const images = new Map();
// Loads (once) an image to draw from: a static path, or a custom halftone
// picture's object URL (appearance.js). `version` goes into the cache key so
// a replaced picture (appearance.js's halftoneVersion) always redraws rather
// than serving what an older object URL happened to load.
export function loadDitherImage(src, version = 0) {
  const key = src + '#' + version;
  if (!images.has(key)) {
    images.set(key, new Promise((resolve, reject) => {
      const img = new Image();
      img.onload = () => resolve(img);
      img.onerror = () => { images.delete(key); reject(new Error("Couldn't load " + src)); };
      img.src = src;
    }));
  }
  return images.get(key);
}

function sampler(w, h) {
  if (typeof OffscreenCanvas === "function") return new OffscreenCanvas(w, h);
  const c = document.createElement("canvas"); c.width = w; c.height = h; return c;
}

// Draws `img` into `canvas` as halftone at W x H device pixels. Returns how
// long it took, in milliseconds.
export function drawDither(canvas, img, W, H, opts = {}) {
  const cell = opts.cell || 4, levels = opts.levels || 10, dotRadius = opts.dotRadius || 0.4;
  const gapShade = opts.gapShade || 0.86, fade = opts.fade || null;
  const fadeColor = opts.palette?.base || opts.fadeColor || [243, 234, 219];  // --k-parchment
  // opts.palette { ink, base }: two-tone, each cell's brightness mapped from
  // the theme's ink (dark parts) to its base (light parts).
  const palette = opts.palette || null;
  const started = performance.now();
  const ctx = canvas.getContext("2d");
  canvas.width = W; canvas.height = H;
  const c = ditherCell(cell);
  const gw = Math.ceil(W / c), gh = Math.ceil(H / c);
  const s = Math.max(gw / img.naturalWidth, gh / img.naturalHeight);
  const sw = gw / s, sh = gh / s;
  const sx = (img.naturalWidth - sw) * (opts.focusX ?? 0.5), sy = (img.naturalHeight - sh) * (opts.focusY ?? 0.5);
  const small = sampler(gw, gh);
  const sctx = small.getContext("2d");
  sctx.imageSmoothingQuality = "high";
  sctx.drawImage(img, sx, sy, sw, sh, 0, 0, gw, gh);
  const px = sctx.getImageData(0, 0, gw, gh).data;
  const r = dotRadius * c;
  ctx.fillStyle = `rgb(${fadeColor})`;
  ctx.fillRect(0, 0, W, H);
  for (let y = 0; y < gh; y++) {
    const f = fade ? Math.min(1, Math.max(0, (y / gh - fade[0]) / (fade[1] - fade[0]))) : 0;
    for (let x = 0; x < gw; x++) {
      const t = BAYER4[(y % 4) * 4 + (x % 4)];
      if (f > t) continue;  // dissolved into the page colour, already painted
      const i = (y * gw + x) * 4;
      let R = px[i] / 255, G = px[i + 1] / 255, B = px[i + 2] / 255;
      const L = 0.299 * R + 0.587 * G + 0.114 * B;
      const Lq = Math.floor(L * (levels - 1) + t) / (levels - 1);
      if (palette) {
        const tone = palette.ink.map((v, n) => v + (palette.base[n] - v) * Lq);
        ctx.fillStyle = `rgb(${tone.map((v, n) => v + (palette.ink[n] - v) * 0.12)})`;
        ctx.fillRect(x * c, y * c, c, c);
        ctx.fillStyle = `rgb(${tone})`;
        ctx.beginPath();
        ctx.arc(x * c + c / 2, y * c + c / 2, r, 0, Math.PI * 2);
        ctx.fill();
        continue;
      }
      const k = (Lq + 0.02) / (L + 0.02);
      R = Math.min(1, R * k); G = Math.min(1, G * k); B = Math.min(1, B * k);
      ctx.fillStyle = `rgb(${R * gapShade * 255},${G * gapShade * 255},${B * gapShade * 255})`;
      ctx.fillRect(x * c, y * c, c, c);
      ctx.fillStyle = `rgb(${Math.min(255, (R * 1.04 + 0.01) * 255)},${Math.min(255, (G * 1.04 + 0.01) * 255)},${Math.min(255, (B * 1.04 + 0.01) * 255)})`;
      ctx.beginPath();
      ctx.arc(x * c + c / 2, y * c + c / 2, r, 0, Math.PI * 2);
      ctx.fill();
    }
  }
  return performance.now() - started;
}

export function mountDither(el, src, opts = {}) {
  const canvas = document.createElement("canvas");
  canvas.className = "dither-canvas";
  canvas.setAttribute("aria-hidden", "true");
  let raf = 0, drawnWidth = 0, observer = null, stopped = false, img = null;
  // opts.onReady: called once, after the first drawing (or when it can't
  // happen), e.g. so a view transition captures the finished banner.
  let pendingReady = opts.onReady;
  const settle = () => { const ready = pendingReady; pendingReady = null; ready?.(); };

  function draw() {
    if (stopped || !img) return;
    const dpr = window.devicePixelRatio || 1;
    const W = Math.max(1, Math.round(el.clientWidth * dpr));
    const H = Math.max(1, Math.round(el.clientHeight * dpr));
    // Phones change height while scrolling (the address bar); only a new
    // width, or a real height change, is worth a full redraw.
    if (W === drawnWidth && canvas.height === H) return;
    drawnWidth = W;
    const ms = drawDither(canvas, img, W, H, opts);
    const first = !canvas.isConnected;
    if (first) el.prepend(canvas);
    el.classList.add("is-dithered");
    // opts.develop: the first drawing appears dot by dot, from a parchment
    // cover cleared in the dither's order (the website hero).
    if (first && opts.develop && !matchMedia("(prefers-reduced-motion: reduce)").matches) {
      const cover = document.createElement("canvas");
      cover.className = "dither-canvas dither-cover";
      cover.width = W; cover.height = H;
      const cctx = cover.getContext("2d");
      cctx.fillStyle = `rgb(${opts.fadeColor || [243, 234, 219]})`;
      cctx.fillRect(0, 0, W, H);
      canvas.after(cover);
      dissolveCells(cctx, ditherCell(opts.cell), opts.develop, () => stopped).then(() => cover.remove());
    }
    el.dataset.drawMs = String(Math.round(ms));  // read by scripts/ui-smoke.cjs
  }

  const schedule = () => { cancelAnimationFrame(raf); raf = requestAnimationFrame(() => { try { draw(); } catch (_) { /* the plain image stays */ } settle(); }); };
  loadDitherImage(src, opts.version || 0).then((loaded) => {
    if (stopped) return;
    img = loaded;
    // Drawn now, not on the next frame: while a view transition is setting
    // up, frames and resize observers wait for it, so a banner drawn there
    // would hold it up (found 2026-10-07: Chat to Home hit its 800 ms cap).
    if (el.clientWidth) { try { draw(); } catch (_) { /* the plain image stays */ } settle(); }
    observer = new ResizeObserver(schedule); observer.observe(el);
  }, settle);  // the plain image stays
  return () => { stopped = true; cancelAnimationFrame(raf); observer?.disconnect(); canvas.remove(); el.classList.remove("is-dithered"); };
}
