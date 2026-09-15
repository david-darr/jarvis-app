import { el, api, toast } from './api.js';
import { marked, DOMPurify, hljs } from './vendor/chat-vendor.js';
import { closeBrowser } from './browserPane.js';

const GENERATED = /^\/generated-(?:images|files)\/[^/?#]+$/;
const TAGS = ['p', 'br', 'strong', 'em', 'del', 's', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'ul', 'ol', 'li', 'blockquote', 'pre', 'code', 'a', 'img', 'table', 'thead', 'tbody', 'tr', 'th', 'td', 'hr', 'input'];

export async function copyText(text) {
  try { await navigator.clipboard.writeText(text); toast('Copied to clipboard', 'success'); }
  catch { toast('Clipboard unavailable. Select the text to copy it.', 'error'); }
}

// Sanitize into a detached fragment BEFORE insertion. No remote image loads,
// raw HTML styles, event handlers, frames, IDs, or application-local links.
export function renderMessageBody(body, text, sessionId, rich = true) {
  body._rawText = text;
  body.classList.toggle('chat-prose', rich);
  if (!rich) { body.textContent = text; return; }
  const fragment = DOMPurify.sanitize(marked.parse(text, { gfm: true, breaks: false }), {
    ALLOWED_TAGS: TAGS, ALLOWED_ATTR: ['href', 'src', 'alt', 'class', 'type', 'checked', 'disabled', 'start'],
    RETURN_DOM_FRAGMENT: true,
  });
  for (const node of fragment.querySelectorAll('[class]')) {
    const language = node.tagName === 'CODE' && [...node.classList].find(c => /^language-[a-z0-9_-]+$/i.test(c));
    node.removeAttribute('class');
    if (language) node.classList.add(language);
  }
  for (const input of fragment.querySelectorAll('input')) {
    input.type = 'checkbox'; input.disabled = true;
  }
  for (const node of fragment.querySelectorAll('a, img')) {
    const url = node.getAttribute(node.tagName === 'IMG' ? 'src' : 'href') || '';
    const label = node.tagName === 'IMG' ? node.getAttribute('alt') : node.textContent;
    if (GENERATED.test(url) && sessionId) {
      let filename;
      try { filename = decodeURIComponent(url.split('/').pop()).replace(/^[a-f0-9]{12}_/, ''); }
      catch { filename = 'Generated file'; }
      const ext = filename.split('.').pop().toUpperCase();
      const card = el('button', { type: 'button', class: 'artifact-card', 'aria-label': `Preview ${filename}`, onclick: () => openArtifact(sessionId, url, filename, card) }, [
        el('span', { class: 'artifact-icon', text: ext.slice(0, 5) }),
        el('span', { class: 'artifact-info' }, [el('strong', { text: filename }), el('span', { text: label && label !== filename ? label : 'Generated file · Preview and download' })]),
        el('span', { class: 'artifact-arrow', text: '↗', 'aria-hidden': 'true' }),
      ]);
      node.replaceWith(card);
    } else if (node.tagName === 'IMG') {
      node.replaceWith(document.createTextNode(label || '[Image]'));
    } else if (/^https?:\/\//i.test(url) || /^mailto:/i.test(url)) {
      node.setAttribute('target', '_blank'); node.setAttribute('rel', 'noopener noreferrer');
    } else {
      node.replaceWith(document.createTextNode(node.textContent));
    }
  }
  for (const table of fragment.querySelectorAll('table')) {
    const wrap = el('div', { class: 'chat-table-wrap', tabindex: '0', role: 'region', 'aria-label': 'Response table' });
    table.replaceWith(wrap); wrap.append(table);
  }
  for (const code of fragment.querySelectorAll('pre > code')) {
    const raw = code.textContent;
    const language = [...code.classList].find(c => c.startsWith('language-'))?.slice(9) || 'text';
    if (raw.length < 100000 && hljs.getLanguage(language)) code.innerHTML = hljs.highlight(raw, { language, ignoreIllegals: true }).value;
    const pre = code.parentElement;
    const block = el('div', { class: 'chat-code-block' });
    pre.replaceWith(block);
    block.append(el('div', { class: 'chat-code-header' }, [
      el('span', { text: language }), el('button', { type: 'button', text: 'Copy code', onclick: () => copyText(raw) }),
    ]), pre);
  }
  body.replaceChildren(fragment);
}

let viewer = null;
export function closeArtifact() {
  if (!viewer) return;
  const { panel, controller, opener, onKey } = viewer;
  viewer = null;
  controller.abort(); panel.remove(); document.removeEventListener('keydown', onKey);
  if (opener?.isConnected) opener.focus({ preventScroll: true });
}

function fileSize(bytes) { return bytes < 1024 ? `${bytes} B` : bytes < 1048576 ? `${(bytes / 1024).toFixed(1)} KB` : `${(bytes / 1048576).toFixed(1)} MB`; }

// -- Office previews (David's ask 2026-09-15) ------------------------------
// The document is parsed by core/office_preview.py and arrives here as plain
// structured data — rows, blocks, slides. Nothing from the file is ever
// treated as markup: every string below goes in through textContent, so a
// spreadsheet cell containing "<script>" is shown as those characters.

function officeGrid(rows, { header = true } = {}) {
  const table = el('table', { class: 'office-grid' });
  rows.forEach((row, index) => {
    const tr = el('tr');
    for (const cell of row) tr.append(el(header && index === 0 ? 'th' : 'td', { text: cell }));
    table.append(tr);
  });
  const wrap = el('div', { class: 'office-grid-wrap', tabindex: '0', role: 'region', 'aria-label': 'Table contents' });
  wrap.append(table);
  return wrap;
}

function officeNote(text) { return el('p', { class: 'office-note', text }); }

function renderSheets(content, actions, data) {
  const sheets = data.sheets || [];
  if (!sheets.length) { content.replaceChildren(officeNote('This workbook has no readable sheets.')); return; }
  const body = el('div', { class: 'office-body' });
  const tabs = el('div', { class: 'office-tabs', role: 'tablist' });
  const show = (index) => {
    const sheet = sheets[index];
    [...tabs.children].forEach((tab, i) => {
      tab.classList.toggle('active', i === index);
      tab.setAttribute('aria-selected', String(i === index));
    });
    const parts = [officeGrid(sheet.rows)];
    // Says plainly that this is a partial view, rather than letting the
    // panel imply the file ends where the preview does.
    if (sheet.truncated_rows || sheet.truncated_cols) {
      parts.push(officeNote(
        `Showing the first ${sheet.rows.length} of ${sheet.total_rows} rows`
        + (sheet.truncated_cols ? ` and the first columns of ${sheet.total_cols}` : '')
        + '. Download the file for everything.'));
    }
    body.replaceChildren(...parts);
  };
  sheets.forEach((sheet, index) => {
    tabs.append(el('button', {
      type: 'button', class: 'office-tab', role: 'tab', text: sheet.name,
      onclick: () => show(index),
    }));
  });
  content.replaceChildren(tabs, body);
  if (data.truncated_sheets) content.append(officeNote('Only the first sheets are shown.'));
  show(0);
}

function renderDoc(content, data) {
  const doc = el('div', { class: 'artifact-document office-doc' });
  for (const block of data.blocks || []) {
    if (block.type === 'table') doc.append(officeGrid(block.rows));
    else if (block.type === 'heading') doc.append(el(`h${Math.min(Math.max(block.level, 1), 6)}`, { text: block.text }));
    else if (block.type === 'list') doc.append(el('p', { class: 'office-list-item', text: block.text }));
    else doc.append(el('p', { text: block.text }));
  }
  if (!doc.childElementCount) doc.append(officeNote('This document has no readable text.'));
  content.replaceChildren(doc);
  if (data.truncated) content.append(officeNote('Only the beginning of this document is shown.'));
}

function renderDeck(content, actions, data) {
  const slides = data.slides || [];
  if (!slides.length) { content.replaceChildren(officeNote('This presentation has no readable slides.')); return; }
  let index = 0;
  const label = el('span', { class: 'muted', 'aria-live': 'polite' });
  const prev = el('button', { type: 'button', class: 'btn quiet', text: '←', 'aria-label': 'Previous slide' });
  const next = el('button', { type: 'button', class: 'btn quiet', text: '→', 'aria-label': 'Next slide' });
  actions.append(prev, label, next);
  const body = el('div', { class: 'office-body' });
  const show = () => {
    const slide = slides[index];
    const card = el('div', { class: 'office-slide' });
    if (slide.title) card.append(el('h2', { text: slide.title }));
    for (const line of slide.body || []) card.append(el('p', { text: line }));
    for (const rows of slide.tables || []) card.append(officeGrid(rows));
    if (!card.childElementCount) card.append(officeNote('This slide has no text.'));
    const parts = [card];
    if (slide.notes) parts.push(el('details', { class: 'office-notes' }, [el('summary', { text: 'Speaker notes' }), el('p', { text: slide.notes })]));
    body.replaceChildren(...parts);
    label.textContent = `${index + 1} / ${slides.length}`;
    prev.disabled = index === 0;
    next.disabled = index >= slides.length - 1;
    body.scrollTop = 0;
  };
  prev.onclick = () => { if (index > 0) { index--; show(); } };
  next.onclick = () => { if (index < slides.length - 1) { index++; show(); } };
  content.replaceChildren(body);
  // Stated up front rather than left for someone to discover: the text and
  // structure are real, the visual design is not reproduced. See
  // core/office_preview.py for why there is no renderer behind this.
  if (data.layout_fidelity === false) {
    content.append(officeNote('Text and structure only — slide layout, theming, and images are not shown. Download the file to see the real slides.'));
  }
  if (data.truncated) content.append(officeNote('Only the first slides are shown.'));
  show();
}

export async function openArtifact(sessionId, url, name, opener) {
  closeArtifact();
  // One right-hand pane at a time. In the desktop app the browser's page is
  // a native layer composited above the HTML, so leaving it open would paint
  // straight over this preview regardless of stacking.
  closeBrowser();
  const host = document.querySelector('.chat-layout');
  if (!host) return;
  const controller = new AbortController();
  const content = el('div', { class: 'artifact-preview', text: 'Loading preview…', 'aria-live': 'polite' });
  const detail = el('span', { class: 'artifact-detail', text: 'Generated in this conversation' });
  const close = el('button', { type: 'button', class: 'btn quiet', text: '×', 'aria-label': 'Close file preview', onclick: closeArtifact });
  const actions = el('div', { class: 'artifact-toolbar' });
  const panel = el('aside', { class: 'artifact-panel', role: 'region', 'aria-label': `File preview: ${name}` }, [
    el('header', { class: 'artifact-header' }, [el('div', {}, [el('h2', { text: name }), detail]), close]), actions, content,
  ]);
  const onKey = event => { if (event.key === 'Escape') { event.preventDefault(); closeArtifact(); } };
  viewer = { panel, controller, opener, onKey };
  document.addEventListener('keydown', onKey); host.append(panel); close.focus({ preventScroll: true });
  const query = new URLSearchParams({ session_id: sessionId, url });
  const contentURL = `/api/chat/artifacts/content?${query}`;
  try {
    const meta = await api(`/api/chat/artifacts?${query}`, { signal: controller.signal });
    if (!panel.isConnected) return;
    detail.textContent = `${meta.extension.toUpperCase() || 'FILE'} · ${fileSize(meta.size)}`;
    actions.append(el('a', { class: 'btn', text: 'Download', href: `${contentURL}&download=true`, download: meta.filename }));
    content.replaceChildren();
    if (meta.kind === 'image') {
      const img = el('img', { src: contentURL, alt: meta.filename });
      img.onerror = () => { content.textContent = 'This image could not be displayed. You can still download the original.'; };
      content.append(img);
    } else if (meta.kind === 'pdf') {
      content.textContent = 'Loading PDF…';
      const pdfjs = await import('./vendor/pdf.mjs');
      if (!panel.isConnected) return;
      pdfjs.GlobalWorkerOptions.workerSrc = '/static/js/vendor/pdf.worker.mjs';
      const task = pdfjs.getDocument({ url: contentURL, isEvalSupported: false, enableXfa: false,
        cMapUrl: '/static/js/vendor/pdf-assets/cmaps/', cMapPacked: true,
        standardFontDataUrl: '/static/js/vendor/pdf-assets/standard_fonts/',
        wasmUrl: '/static/js/vendor/pdf-assets/wasm/' });
      controller.signal.addEventListener('abort', () => { task.destroy().catch(() => {}); }, { once: true });
      const pdf = await task.promise;
      if (!panel.isConnected) return;
      let pageNumber = 1, rendering = false;
      const pageLabel = el('span', { class: 'muted', 'aria-live': 'polite' });
      const prev = el('button', { type: 'button', class: 'btn quiet', text: '←', 'aria-label': 'Previous PDF page' });
      const next = el('button', { type: 'button', class: 'btn quiet', text: '→', 'aria-label': 'Next PDF page' });
      actions.append(prev, pageLabel, next);
      const renderPage = async () => {
        if (rendering || !panel.isConnected) return;
        rendering = true; prev.disabled = next.disabled = true;
        try {
          const page = await pdf.getPage(pageNumber);
          if (!panel.isConnected) return;
          const natural = page.getViewport({ scale: 1 });
          const width = Math.max(200, content.clientWidth - 40);
          const scale = Math.min(width / natural.width, 2);
          const viewport = page.getViewport({ scale });
          const pixelRatio = Math.min(devicePixelRatio || 1, 2);
          const canvas = el('canvas', { class: 'artifact-pdf-page', role: 'img', 'aria-label': `${meta.filename}, page ${pageNumber} of ${pdf.numPages}` });
          canvas.width = Math.floor(viewport.width * pixelRatio); canvas.height = Math.floor(viewport.height * pixelRatio);
          canvas.style.width = '100%'; canvas.style.height = 'auto';
          await page.render({ canvasContext: canvas.getContext('2d'), viewport, transform: [pixelRatio, 0, 0, pixelRatio, 0, 0] }).promise;
          if (!panel.isConnected) return;
          const text = (await page.getTextContent()).items.map(item => item.str || '').join(' ');
          content.replaceChildren(canvas, el('details', { class: 'artifact-pdf-text' }, [el('summary', { text: 'Page text' }), el('p', { text })]));
          pageLabel.textContent = `${pageNumber} / ${pdf.numPages}`;
          content.scrollTop = 0;
        } catch (error) {
          if (panel.isConnected) content.textContent = 'This PDF could not be rendered. Download the original to open it.';
        } finally { rendering = false; prev.disabled = pageNumber <= 1; next.disabled = pageNumber >= pdf.numPages; }
      };
      prev.onclick = () => { if (!rendering && pageNumber > 1) { pageNumber--; renderPage(); } };
      next.onclick = () => { if (!rendering && pageNumber < pdf.numPages) { pageNumber++; renderPage(); } };
      await renderPage();
    } else if (meta.kind === 'office') {
      content.textContent = 'Reading document…';
      const data = await api(`/api/chat/artifacts/office?${query}`, { signal: controller.signal });
      if (!panel.isConnected) return;
      if (data.kind === 'xlsx') {
        renderSheets(content, actions, data);
      } else if (data.kind === 'docx') {
        renderDoc(content, data);
      } else if (data.kind === 'pptx') {
        renderDeck(content, actions, data);
      } else if (data.kind === 'csv') {
        // A grid by default, with the raw file one click away — the parsed
        // view is the useful one, but CSV is still a text file and someone
        // may want to see exactly what is in it.
        const gridView = () => {
          const parts = [officeGrid(data.rows)];
          if (data.truncated) parts.push(officeNote('Only the first rows are shown. Download the file for everything.'));
          content.replaceChildren(...parts);
        };
        const gridBtn = el('button', { type: 'button', class: 'btn quiet', text: 'Table', 'aria-pressed': 'true' });
        const rawBtn = el('button', { type: 'button', class: 'btn quiet', text: 'Source', 'aria-pressed': 'false' });
        gridBtn.onclick = () => { gridView(); gridBtn.setAttribute('aria-pressed', 'true'); rawBtn.setAttribute('aria-pressed', 'false'); };
        rawBtn.onclick = async () => {
          gridBtn.setAttribute('aria-pressed', 'false'); rawBtn.setAttribute('aria-pressed', 'true');
          try {
            const res = await fetch(contentURL, { signal: controller.signal });
            if (!res.ok) throw new Error('The file could not be loaded');
            const text = await res.text();
            if (panel.isConnected) content.replaceChildren(el('pre', { class: 'artifact-source', text }));
          } catch (error) {
            if (error.name !== 'AbortError' && panel.isConnected) content.textContent = 'The original file could not be loaded.';
          }
        };
        actions.append(gridBtn, rawBtn);
        gridView();
      } else {
        content.replaceChildren(officeNote('This file has no structured preview.'));
      }
    } else if (['text', 'markdown', 'html'].includes(meta.kind)) {
      const res = await fetch(contentURL, { signal: controller.signal });
      if (!res.ok) throw new Error('The file could not be loaded');
      const text = await res.text();
      if (!panel.isConnected) return;
      const source = () => { content.replaceChildren(el('pre', { class: 'artifact-source', text })); };
      const preview = () => {
        if (meta.kind === 'html') {
          const frame = el('iframe', { sandbox: '', title: meta.filename, class: 'artifact-frame', referrerpolicy: 'no-referrer' });
          // Opaque origin, no scripts, navigation, forms, remote assets, or
          // application access. Static HTML/CSS preview, never an app runtime.
          const safe = DOMPurify.sanitize(text, { WHOLE_DOCUMENT: true, FORBID_TAGS: ['script', 'iframe', 'object', 'embed', 'form', 'base', 'meta', 'link', 'a'], FORBID_ATTR: ['srcset', 'src', 'href', 'poster', 'background'] });
          frame.srcdoc = `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:; form-action 'none'; base-uri 'none'"><style>body{margin:24px;font-family:system-ui;color:#202124;background:white}</style>${safe}`;
          content.replaceChildren(frame);
        } else if (meta.kind === 'markdown') {
          const prose = el('div', { class: 'artifact-document' }); renderMessageBody(prose, text, null); content.replaceChildren(prose);
        } else source();
      };
      if (meta.kind !== 'text') {
        const previewBtn = el('button', { type: 'button', class: 'btn quiet', text: 'Preview', 'aria-pressed': 'true' });
        const sourceBtn = el('button', { type: 'button', class: 'btn quiet', text: 'Source', 'aria-pressed': 'false' });
        previewBtn.onclick = () => { preview(); previewBtn.setAttribute('aria-pressed', 'true'); sourceBtn.setAttribute('aria-pressed', 'false'); };
        sourceBtn.onclick = () => { source(); previewBtn.setAttribute('aria-pressed', 'false'); sourceBtn.setAttribute('aria-pressed', 'true'); };
        actions.append(previewBtn, sourceBtn);
      }
      actions.append(el('button', { type: 'button', class: 'btn quiet', text: 'Copy', onclick: () => copyText(text) }));
      preview();
    } else {
      content.append(el('div', { class: 'artifact-fallback' }, [el('h3', { text: 'Your file is ready' }), el('p', { text: 'This format or file size has no in-app preview. Download the original to open it in its own application.' })]));
    }
  } catch (error) {
    if (error.name !== 'AbortError' && panel.isConnected) content.textContent = `Preview unavailable. ${error.message}`;
  }
}
