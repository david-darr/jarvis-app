import { el } from './api.js';

// The original five arguments remain valid for Google Drive. Artifact-only
// thumbnails/zoom are enabled with the optional sixth argument.
export async function renderPDF(content, actions, contentURL, name, controller, { artifacts = false } = {}) {
  content.replaceChildren(el('div', { class: 'artifact-skeleton', role: 'status', 'aria-label': 'Loading PDF' }, Array.from({ length: 4 }, () => el('span'))));
  const pdfjs = await import('./vendor/pdf.mjs');
  if ((!artifacts && !content.isConnected) || controller.signal.aborted) return null;
  pdfjs.GlobalWorkerOptions.workerSrc = '/static/js/vendor/pdf.worker.mjs';
  const task = pdfjs.getDocument({ url: contentURL, isEvalSupported: false, enableXfa: false,
    cMapUrl: '/static/js/vendor/pdf-assets/cmaps/', cMapPacked: true,
    standardFontDataUrl: '/static/js/vendor/pdf-assets/standard_fonts/', wasmUrl: '/static/js/vendor/pdf-assets/wasm/' });
  let activeRender = null, thumbRender = null, observer = null, intersection = null, timer = null;
  controller.signal.addEventListener('abort', () => {
    clearTimeout(timer); observer?.disconnect(); intersection?.disconnect(); activeRender?.cancel(); thumbRender?.cancel(); task.destroy().catch(() => {});
  }, { once: true });
  let pdf;
  try { pdf = await task.promise; }
  catch (error) {
    if (artifacts && ([400, 404].includes(error.status) || error.name === 'MissingPDFException')) {
      throw new Error(`${error.status || 404}: ${error.message}`);
    }
    throw error;
  }
  if ((!artifacts && !content.isConnected) || controller.signal.aborted) return null;
  let pageNumber = 1, mode = 'width', zoom = 1, generation = 0, rendering = false, pending = false;
  const root = el('div', { class: artifacts ? 'artifact-pdf-viewer' : 'artifact-pdf-viewer drive-pdf-viewer' });
  const rail = el('div', { class: 'artifact-thumbnail-rail', 'aria-label': 'Page thumbnails' });
  const body = el('div', { class: 'artifact-pdf-body' });
  const pageLabel = el('span', { class: 'muted', 'aria-live': 'polite' });
  const percentage = el('span', { class: 'muted', 'aria-live': 'polite' });
  const btn = (text, label, onclick) => el('button', { type: 'button', class: 'btn quiet', text, 'aria-label': label, onclick });
  const prev = btn('←', 'Previous PDF page', () => navigate(pageNumber - 1)), next = btn('→', 'Next PDF page', () => navigate(pageNumber + 1));
  const fitWidth = btn('Fit width', 'Fit width', () => { mode = 'width'; requestRender(); });
  const fitPage = btn('Fit page', 'Fit page', () => { mode = 'page'; requestRender(); });
  const changeZoom = factor => { mode = 'zoom'; zoom = Math.min(4, Math.max(.1, zoom * factor)); requestRender(); };
  actions.append(prev, pageLabel, next);
  if (artifacts) actions.append(fitWidth, fitPage, btn('−', 'Zoom out', () => changeZoom(1 / 1.25)), btn('+', 'Zoom in', () => changeZoom(1.25)), percentage);
  root.append(...(artifacts ? [rail, body] : [body])); content.replaceChildren(root);
  // Drive and Unit C can inspect the same root without depending on callers.
  content.viewerRoot = root;
  const requestRender = () => { generation++; pending = true; activeRender?.cancel(); if (!rendering) void renderPage(); };
  const navigate = n => { if (n >= 1 && n <= pdf.numPages) { pageNumber = n; requestRender(); } };
  const renderPage = async () => {
    if ((!artifacts && !content.isConnected) || controller.signal.aborted) return;
    rendering = true; pending = false;
    const ticket = generation, n = pageNumber;
    try {
      const page = await pdf.getPage(n);
      if (ticket !== generation || controller.signal.aborted) return;
      const natural = page.getViewport({ scale: 1 });
      const availableWidth = Math.max(100, body.clientWidth || (content.closest('.artifact-panel')?.clientWidth || 300) - (artifacts ? 180 : 40));
      const availableHeight = Math.max(100, (content.clientHeight || 700) - 70);
      const scale = mode === 'zoom' ? zoom : Math.min(4, availableWidth / natural.width, ...(mode === 'page' ? [availableHeight / natural.height] : []));
      zoom = scale;
      const viewport = page.getViewport({ scale });
      const ratio = Math.min(devicePixelRatio || 1, 2, Math.sqrt(16_000_000 / (viewport.width * viewport.height)));
      const canvas = el('canvas', { class: 'artifact-pdf-page', role: 'img', 'aria-label': `${name}, page ${n} of ${pdf.numPages}` });
      canvas.width = Math.max(1, Math.floor(viewport.width * ratio)); canvas.height = Math.max(1, Math.floor(viewport.height * ratio));
      canvas.style.width = `${viewport.width}px`; canvas.style.height = `${viewport.height}px`;
      activeRender = page.render({ canvasContext: canvas.getContext('2d'), viewport, transform: [ratio, 0, 0, ratio, 0, 0] });
      await activeRender.promise;
      activeRender = null;
      if (ticket !== generation || controller.signal.aborted || (!artifacts && !content.isConnected)) return;
      const text = (await page.getTextContent()).items.map(item => item.str || '').join(' ');
      if (ticket !== generation || controller.signal.aborted) return;
      const wrapper = el('div', { class: 'artifact-pdf-page-wrap', 'data-pick': `pdf:page=${n}` }, [canvas]);
      body.replaceChildren(wrapper, el('details', { class: 'artifact-pdf-text' }, [el('summary', { text: 'Page text' }), el('p', { text })]));
      body._pageText = text;
      pageLabel.textContent = `${n} / ${pdf.numPages}`; percentage.textContent = `${Math.round(scale * 100)}%`;
      fitWidth.setAttribute('aria-pressed', String(mode === 'width')); fitPage.setAttribute('aria-pressed', String(mode === 'page'));
      prev.disabled = n <= 1; next.disabled = n >= pdf.numPages;
      [...rail.children].forEach((thumb, i) => { thumb.classList.toggle('active', i + 1 === n); thumb.setAttribute('aria-current', i + 1 === n ? 'page' : 'false'); });
    } catch (error) {
      if (error.name !== 'RenderingCancelledException' && ticket === generation && (artifacts || content.isConnected) && !controller.signal.aborted) body.textContent = 'This PDF could not be rendered. Download the original to open it.';
    } finally { rendering = false; if (pending && !controller.signal.aborted) void renderPage(); }
  };
  if (artifacts) {
    const copy = btn('Copy', 'Copy page text', () => import('./chatContent.js').then(m => m.copyText(body._pageText || '')));
    actions.append(copy);
    // Only visible thumbnails render, one at a time, to bound canvas work.
    const queue = [], queued = new Set();
    let busy = false;
    const drain = async () => {
      if (busy) return;
      busy = true;
      try {
        while (queue.length && !controller.signal.aborted) {
          const thumb = queue.shift(), n = Number(thumb.dataset.page);
          try {
            const page = await pdf.getPage(n);
            if (controller.signal.aborted) break;
            const natural = page.getViewport({ scale: 1 });
            const viewport = page.getViewport({ scale: Math.min(100 / natural.width, 130 / natural.height) });
            const canvas = el('canvas', { 'aria-hidden': 'true' }); canvas.width = Math.ceil(viewport.width); canvas.height = Math.ceil(viewport.height);
            thumbRender = page.render({ canvasContext: canvas.getContext('2d'), viewport }); await thumbRender.promise; thumbRender = null;
            if (!controller.signal.aborted) thumb.prepend(canvas);
          } catch { /* navigation still works if a thumbnail cannot render */ }
        }
      } finally { busy = false; }
    };
    intersection = new IntersectionObserver(entries => {
      for (const entry of entries) if (entry.isIntersecting && !queued.has(entry.target)) { queued.add(entry.target); queue.push(entry.target); intersection.unobserve(entry.target); }
      void drain();
    }, { root: rail, rootMargin: '120px' });
    for (let n = 1; n <= pdf.numPages; n++) {
      const thumb = btn(String(n), `Go to page ${n}`, () => navigate(n)); thumb.classList.add('artifact-thumbnail'); thumb.dataset.page = String(n); rail.append(thumb); intersection.observe(thumb);
    }
    let width = content.clientWidth, height = content.clientHeight;
    observer = new ResizeObserver(() => {
      // Parking a document is not a PDF resize. Avoid replacing its canvas
      // and resetting scroll while the pane is hidden or off the Chat view.
      if (!content.clientWidth || !content.clientHeight) return;
      if (width === content.clientWidth && height === content.clientHeight) return;
      width = content.clientWidth; height = content.clientHeight;
      clearTimeout(timer); timer = setTimeout(requestRender, 150);
    }); observer.observe(content);
  }
  await renderPage();
  return root;
}
