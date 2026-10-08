import { el } from './api.js';

// Shared page renderer for Chat artifacts and Google Drive previews.
export async function renderPDF(content, actions, contentURL, name, controller) {
  content.textContent = 'Loading PDF…';
  const pdfjs = await import('./vendor/pdf.mjs');
  if ((!content.isConnected || controller.signal.aborted)) return;
  pdfjs.GlobalWorkerOptions.workerSrc = '/static/js/vendor/pdf.worker.mjs';
  const task = pdfjs.getDocument({ url: contentURL, isEvalSupported: false, enableXfa: false,
    cMapUrl: '/static/js/vendor/pdf-assets/cmaps/', cMapPacked: true,
    standardFontDataUrl: '/static/js/vendor/pdf-assets/standard_fonts/',
    wasmUrl: '/static/js/vendor/pdf-assets/wasm/' });
  controller.signal.addEventListener('abort', () => { task.destroy().catch(() => {}); }, { once: true });
  const pdf = await task.promise;
  if ((!content.isConnected || controller.signal.aborted)) return;
  let pageNumber = 1, rendering = false;
  const pageLabel = el('span', { class: 'muted', 'aria-live': 'polite' });
  const prev = el('button', { type: 'button', class: 'btn quiet', text: '←', 'aria-label': 'Previous PDF page' });
  const next = el('button', { type: 'button', class: 'btn quiet', text: '→', 'aria-label': 'Next PDF page' });
  actions.append(prev, pageLabel, next);
  const renderPage = async () => {
    if (rendering || (!content.isConnected || controller.signal.aborted)) return;
    rendering = true; prev.disabled = next.disabled = true;
    try {
      const page = await pdf.getPage(pageNumber);
      if ((!content.isConnected || controller.signal.aborted)) return;
      const natural = page.getViewport({ scale: 1 });
      const width = Math.max(200, content.clientWidth - 40);
      const scale = Math.min(width / natural.width, 2);
      const viewport = page.getViewport({ scale });
      const pixelRatio = Math.min(devicePixelRatio || 1, 2);
      const canvas = el('canvas', { class: 'artifact-pdf-page', role: 'img', 'aria-label': `${name}, page ${pageNumber} of ${pdf.numPages}` });
      canvas.width = Math.floor(viewport.width * pixelRatio); canvas.height = Math.floor(viewport.height * pixelRatio);
      canvas.style.width = '100%'; canvas.style.height = 'auto';
      await page.render({ canvasContext: canvas.getContext('2d'), viewport, transform: [pixelRatio, 0, 0, pixelRatio, 0, 0] }).promise;
      if ((!content.isConnected || controller.signal.aborted)) return;
      const text = (await page.getTextContent()).items.map(item => item.str || '').join(' ');
      content.replaceChildren(canvas, el('details', { class: 'artifact-pdf-text' }, [el('summary', { text: 'Page text' }), el('p', { text })]));
      pageLabel.textContent = `${pageNumber} / ${pdf.numPages}`;
      content.scrollTop = 0;
    } catch (error) {
      if (content.isConnected && !controller.signal.aborted) content.textContent = 'This PDF could not be rendered. Download the original to open it.';
    } finally { rendering = false; prev.disabled = pageNumber <= 1; next.disabled = pageNumber >= pdf.numPages; }
  };
  prev.onclick = () => { if (!rendering && pageNumber > 1) { pageNumber--; renderPage(); } };
  next.onclick = () => { if (!rendering && pageNumber < pdf.numPages) { pageNumber++; renderPage(); } };
  await renderPage();
}
