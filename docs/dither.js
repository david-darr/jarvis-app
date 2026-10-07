// Kairos halftone background (brand handoff, BRAND.md 6.8; adapted from its
// code/ditherBackground.js). Draws an image as round dots on a 4x4 ordered
// dither of brightness, at the element's real device-pixel size, so the dot
// grid is never resampled (a scaled dot image shows moire). The pre-rendered
// image under the canvas shows until the first draw, and stays if drawing
// can't happen.
(function () {
  const BAYER4 = [0, 8, 2, 10, 12, 4, 14, 6, 3, 11, 1, 9, 15, 7, 13, 5].map((v) => v / 16 + 1 / 32);

  function mount(el, src, opts) {
    const cell = opts.cell || 4, levels = opts.levels || 10, dotRadius = opts.dotRadius || 0.4;
    const gapShade = opts.gapShade || 0.86, fade = opts.fade || null;
    const fadeColor = opts.fadeColor || [243, 234, 219];
    const canvas = document.createElement("canvas");
    canvas.className = "dither-canvas";
    canvas.setAttribute("aria-hidden", "true");
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const img = new Image();
    let raf = 0, drawnWidth = 0;

    function sampler(w, h) {
      if (typeof OffscreenCanvas === "function") return new OffscreenCanvas(w, h);
      const c = document.createElement("canvas"); c.width = w; c.height = h; return c;
    }

    function draw() {
      const dpr = window.devicePixelRatio || 1;
      const W = Math.max(1, Math.round(el.clientWidth * dpr));
      const H = Math.max(1, Math.round(el.clientHeight * dpr));
      // Phones change height while scrolling (the address bar); only a new
      // width is worth a full redraw.
      if (W === drawnWidth && canvas.height === H) return;
      drawnWidth = W;
      const started = performance.now();
      canvas.width = W; canvas.height = H;
      const c = Math.max(2, Math.round(cell * dpr));
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
          if (f > t) continue; // dissolved into the page colour, already painted
          const i = (y * gw + x) * 4;
          let R = px[i] / 255, G = px[i + 1] / 255, B = px[i + 2] / 255;
          const L = 0.299 * R + 0.587 * G + 0.114 * B;
          const Lq = Math.floor(L * (levels - 1) + t) / (levels - 1);
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
      if (!canvas.isConnected) el.appendChild(canvas);
      el.classList.add("is-dithered");
      el.dataset.drawMs = String(Math.round(performance.now() - started)); // read by scripts/ui-smoke.cjs
    }

    const schedule = () => { cancelAnimationFrame(raf); raf = requestAnimationFrame(() => { try { draw(); } catch (_) { /* the fallback image stays */ } }); };
    img.onload = () => { new ResizeObserver(schedule).observe(el); schedule(); };
    img.src = src;
  }

  window.kairosDither = mount;
})();
