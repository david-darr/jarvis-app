import { el, api } from './api.js';
import { DOMPurify, marked, hljs } from './vendor/chat-vendor.js';
import { renderPDF } from './pdfViewer.js';
import { renderMessageBody, copyText } from './chatContent.js';

// Unit C's hook: content.viewerRoot always points at the current renderer's
// root (including after source/sheet/slide changes). HTML's root is its body.
export function exposeRoot(content, root) {
  content.viewerRoot = root;
  content.dispatchEvent(new CustomEvent('artifact-root', { bubbles: true, detail: { root } }));
  return root;
}
export function loadingSkeleton(content) {
  exposeRoot(content, null);
  content.replaceChildren(el('div', { class: 'artifact-skeleton', role: 'status', 'aria-label': 'Loading preview' },
    Array.from({ length: 5 }, () => el('span', { 'aria-hidden': 'true' }))));
}
const button = (text, onclick, label = text) => el('button', { type: 'button', class: 'btn quiet', text, 'aria-label': label, onclick });
const note = text => el('p', { class: 'office-note', text });
const rgb = value => typeof value === 'string' && /^#[a-f0-9]{6}$/i.test(value) ? value : null;
const number = (value, min, max, fallback = 0) => typeof value === 'number' && Number.isFinite(value) && value >= min && value <= max ? value : fallback;
const imageURI = value => typeof value === 'string' && value.length <= 1536 * 1024 && /^data:image\/(png|jpeg);base64,[A-Za-z0-9+/]+=*$/.test(value) ? value : null;
function fileSize(bytes) { return bytes < 1024 ? `${bytes} B` : bytes < 1048576 ? `${(bytes / 1024).toFixed(1)} KB` : `${(bytes / 1048576).toFixed(1)} MB`; }
export function columnLetter(index) {
  let name = '';
  for (let n = index + 1; n > 0; n = Math.floor((n - 1) / 26)) name = String.fromCharCode(65 + (n - 1) % 26) + name;
  return name;
}
export function officeGrid(rows = [], { sheet = null } = {}) {
  const table = el('table', { class: 'office-grid' });
  const width = Math.max(0, ...rows.map(row => row.length));
  if (sheet !== null) table.append(el('thead', {}, [el('tr', {}, [el('th', { text: '', 'aria-label': 'Row number' }),
    ...Array.from({ length: width }, (_, c) => el('th', { text: columnLetter(c), scope: 'col' }))])]));
  const body = el('tbody');
  rows.forEach((row, r) => {
    const tr = el('tr');
    if (sheet !== null) tr.append(el('th', { text: String(r + 1), scope: 'row' }));
    // Document and slide tables keep their first row as the header; sheets
    // don't, because row 1 there is just row 1 and has its own row number.
    const cell = sheet === null && r === 0 ? 'th' : 'td';
    for (let c = 0; c < width; c++) tr.append(el(cell, { text: row[c] ?? '', ...(sheet !== null ? {
      'data-pick': `xlsx:sheet=${encodeURIComponent(sheet)};cell=${columnLetter(c)}${r + 1}` } : {}) }));
    body.append(tr);
  });
  table.append(body);
  return el('div', { class: 'office-grid-wrap', tabindex: '0', role: 'region', 'aria-label': 'Table contents' }, [table]);
}
function renderRuns(target, runs, fallback = '', slideWidth = null) {
  if (!runs?.length) { target.textContent = fallback; return; }
  for (const run of runs) {
    const span = el('span', { text: String(run.text ?? '') });
    if (run.bold === true) span.style.fontWeight = '700';
    if (run.italic === true) span.style.fontStyle = 'italic';
    if (run.underline === true) span.style.textDecoration = 'underline';
    if (rgb(run.color)) span.style.color = rgb(run.color);
    if (slideWidth && number(run.size_pt, 1, 400, null)) span.style.fontSize = `${run.size_pt * 12700 / slideWidth * 100}cqw`;
    target.append(span);
  }
}
export function renderSheets(content, actions, data) {
  const sheets = data.sheets || [];
  const root = el('div', { class: 'office-workbook' });
  const body = el('div', { class: 'office-body' });
  const tabs = el('div', { class: 'office-tabs', role: 'tablist', 'aria-label': 'Sheets' });
  const show = index => {
    const sheet = sheets[index];
    [...tabs.children].forEach((tab, i) => { tab.classList.toggle('active', i === index); tab.setAttribute('aria-selected', String(i === index)); });
    body.replaceChildren(officeGrid(sheet.rows, { sheet: sheet.name }));
    if (sheet.truncated_rows || sheet.truncated_cols) body.append(note(`Showing ${sheet.rows.length} of ${sheet.total_rows} rows${sheet.truncated_cols ? ` and the first columns of ${sheet.total_cols}` : ''}. Download for everything.`));
  };
  sheets.forEach((sheet, index) => tabs.append(el('button', { type: 'button', class: 'office-tab', role: 'tab', text: sheet.name, onclick: () => show(index) })));
  root.append(tabs, body);
  if (data.truncated_sheets) root.append(note('Only the first sheets are shown.'));
  if (sheets.length) show(0); else body.append(note('This workbook has no readable sheets.'));
  content.replaceChildren(root);
  return exposeRoot(content, root);
}
export function renderDoc(content, data) {
  const root = el('div', { class: 'artifact-document office-doc' });
  let lists = [];
  (data.blocks || []).forEach((block, index) => {
    let node;
    if (block.type !== 'list') lists = [];
    if (block.type === 'table') node = officeGrid(block.rows);
    else if (block.type === 'image') node = imageURI(block.image) ? el('figure', {}, [el('img', { src: block.image, alt: block.text })]) : note(block.text || 'Inline picture (unavailable)');
    else {
      node = el(block.type === 'heading' ? `h${number(block.level, 1, 6, 1)}` : block.type === 'list' ? 'li' : 'p');
      renderRuns(node, block.runs, block.text);
    }
    node.dataset.pick = `docx:block=${index}`;
    if (block.type === 'list') {
      const level = number(block.level, 0, 8), tag = block.list === 'number' ? 'ol' : 'ul';
      lists = lists.slice(0, level + 1);
      if (lists[level]?.tagName.toLowerCase() !== tag) lists = lists.slice(0, level);
      while (lists.length <= level) {
        const list = el(lists.length === level ? tag : 'ul');
        const parent = lists.at(-1);
        if (parent) {
          let item = parent.lastElementChild;
          if (!item) { item = el('li', { class: 'office-list-bridge' }); parent.append(item); }
          item.append(list);
        } else root.append(list);
        lists.push(list);
      }
      lists[level].append(node);
    } else root.append(node);
  });
  if (!root.childElementCount) root.append(note('This document has no readable text.'));
  if (data.truncated) root.append(note('Only the beginning of this document is shown.'));
  content.replaceChildren(root);
  return exposeRoot(content, root);
}
function slideCanvas(data, slide, selectable = true) {
  const width = number(data.slide_width, 1, 1e10, 9144000), height = number(data.slide_height, 1, 1e10, 5143500);
  const canvas = el('div', { class: 'office-slide', role: 'group', 'aria-label': `Slide ${slide.index}` });
  canvas.style.aspectRatio = `${width} / ${height}`;
  if (rgb(slide.background)) canvas.style.backgroundColor = rgb(slide.background);
  for (const group of slide.groups || []) {
    if (!selectable) continue;
    const box = el('div', { class: 'office-pick-group', 'data-pick': `pptx:slide=${slide.index};shape=${group.id}` });
    const children = slide.shapes.filter(s => group.children.includes(s.id));
    if (!children.length) continue;
    const x = Math.min(...children.map(s => s.x)), y = Math.min(...children.map(s => s.y));
    box.style.left = `${x / width * 100}%`; box.style.top = `${y / height * 100}%`;
    box.style.width = `${(Math.max(...children.map(s => s.x+s.w)) - x) / width * 100}%`;
    box.style.height = `${(Math.max(...children.map(s => s.y+s.h)) - y) / height * 100}%`;
    canvas.append(box);
  }
  for (const shape of [...(slide.shapes || [])].sort((a, b) => number(a.z, 0, 10000) - number(b.z, 0, 10000))) {
    const box = el('div', { class: 'office-shape', 'aria-label': `${shape.kind}: ${shape.name}` });
    if (selectable) box.dataset.pick = `pptx:slide=${slide.index};shape=${number(shape.id, 0, 2147483647)}`;
    if (selectable && shape.group != null) box.dataset.group = `pptx:slide=${slide.index};shape=${shape.group}`;
    box.style.left = `${number(shape.x, -1e10, 1e10) / width * 100}%`;
    box.style.top = `${number(shape.y, -1e10, 1e10) / height * 100}%`;
    box.style.width = `${number(shape.w, 0, 1e10) / width * 100}%`;
    box.style.height = `${number(shape.h, 0, 1e10) / height * 100}%`;
    box.style.transform = `rotate(${number(shape.rotation, -360, 360)}deg)`;
    box.style.zIndex = String(number(shape.z, 0, 10000));
    if (rgb(shape.fill)) box.style.backgroundColor = rgb(shape.fill);
    if (rgb(shape.line)) { box.style.borderColor = rgb(shape.line); box.style.borderStyle = 'solid'; }
    if (shape.kind === 'picture' && imageURI(shape.image)) box.append(el('img', { src: shape.image, alt: shape.name }));
    else if (shape.kind === 'table') box.append(officeGrid(shape.rows));
    else if (['text', 'placeholder'].includes(shape.kind) && shape.paragraphs?.length) {
      shape.paragraphs.forEach(paragraph => {
        const p = el('p');
        if (['left', 'center', 'right', 'justify'].includes(paragraph.align)) p.style.textAlign = paragraph.align;
        p.style.paddingLeft = `${number(paragraph.level, 0, 8)}em`;
        renderRuns(p, paragraph.runs, '', width);
        box.append(p);
      });
    } else {
      box.classList.add('office-shape-placeholder');
      box.textContent = `${shape.kind || 'other'}: ${shape.name || 'Unavailable shape'}`;
    }
    canvas.append(box);
  }
  return canvas;
}
export function renderDeck(content, actions, data, controller) {
  const slides = (data.slides || []).map((slide, i) => ({ ...slide, index: i + 1 }));
  const root = el('div', { class: 'office-deck', tabindex: '0', 'aria-label': 'Slides; use arrow keys to navigate' });
  const rail = el('div', { class: 'artifact-thumbnail-rail', 'aria-label': 'Slide thumbnails' });
  const body = el('div', { class: 'office-body' });
  const label = el('span', { class: 'muted', 'aria-live': 'polite' });
  let index = 0;
  const prev = button('←', () => show(index - 1), 'Previous slide'), next = button('→', () => show(index + 1), 'Next slide');
  const show = n => {
    if (n < 0 || n >= slides.length) return;
    index = n;
    body.replaceChildren(slideCanvas(data, slides[index]));
    if (slides[index].notes) body.append(el('details', { class: 'office-notes' }, [el('summary', { text: 'Speaker notes' }), el('p', { text: slides[index].notes })]));
    if (slides[index].truncated_shapes) body.append(note('Only the first shapes are shown.'));
    label.textContent = `${index + 1} / ${slides.length}`;
    prev.disabled = index === 0; next.disabled = index === slides.length - 1;
    [...rail.children].forEach((thumb, i) => { thumb.classList.toggle('active', i === index); thumb.setAttribute('aria-current', i === index ? 'page' : 'false'); });
  };
  slides.forEach((slide, i) => {
    const thumb = button(String(i + 1), () => show(i), `Go to slide ${i + 1}`);
    thumb.classList.add('artifact-thumbnail');
    thumb.prepend(slideCanvas(data, slide, false)); rail.append(thumb);
  });
  root.append(rail, body);
  const key = event => {
    if (!content.closest('.artifact-tab-panel')?.contains(document.activeElement) || /INPUT|TEXTAREA|SELECT/.test(event.target.tagName)) return;
    const delta = ['ArrowLeft', 'ArrowUp', 'PageUp'].includes(event.key) ? -1 : ['ArrowRight', 'ArrowDown', 'PageDown'].includes(event.key) ? 1 : 0;
    if (delta) { event.preventDefault(); show(index + delta); }
  };
  // Pane controls also retain keyboard navigation after a thumbnail click.
  const pane = content.closest('.artifact-tab-panel') || root;
  pane.addEventListener('keydown', key, { signal: controller.signal });
  actions.append(prev, label, next);
  content.replaceChildren(root, note('Approximate layout. Download for the exact slides.'));
  if (!slides.length) body.append(note('This presentation has no readable slides.')); else show(0);
  if (data.truncated) content.append(note('Only the first slides are shown.'));
  return exposeRoot(content, root);
}
export function renderImage(content, actions, meta, contentURL, controller, onError) {
  const root = el('div', { class: 'artifact-image-viewport', tabindex: '0', 'data-pick': 'image' });
  const img = el('img', { src: contentURL, alt: meta.filename, draggable: 'false', hidden: true });
  const skeleton = el('div');
  loadingSkeleton(skeleton);
  const label = el('span', { class: 'muted', 'aria-live': 'polite' });
  let zoom = 1, fit = true, drag = null;
  const paint = () => {
    if (!root.clientWidth || !root.clientHeight) return;
    const fitScale = Math.min(1, Math.max(1, root.clientWidth - 24) / (img.naturalWidth || 1), Math.max(1, root.clientHeight - 24) / (img.naturalHeight || 1));
    if (fit) zoom = fitScale;
    img.style.width = `${(img.naturalWidth || 1) * zoom}px`;
    img.style.height = `${(img.naturalHeight || 1) * zoom}px`;
    root.classList.toggle('is-zoomed', zoom > fitScale);
    label.textContent = `${Math.round(zoom * 100)}%`;
    fitBtn.setAttribute('aria-pressed', String(fit)); actualBtn.setAttribute('aria-pressed', String(!fit && zoom === 1));
  };
  const change = factor => { fit = false; zoom = Math.min(8, Math.max(.05, zoom * factor)); paint(); };
  const fitBtn = button('Fit', () => { fit = true; paint(); root.scrollTo(0, 0); });
  const actualBtn = button('100%', () => { fit = false; zoom = 1; paint(); });
  actions.append(fitBtn, actualBtn, button('−', () => change(1 / 1.25), 'Zoom out'), button('+', () => change(1.25), 'Zoom in'), label);
  root.addEventListener('wheel', event => { if (event.ctrlKey) { event.preventDefault(); change(event.deltaY < 0 ? 1.15 : 1 / 1.15); } }, { passive: false, signal: controller.signal });
  root.addEventListener('pointerdown', event => { if (root.classList.contains('is-zoomed') && event.button === 0 && !content.closest('[data-selecting="true"]')) { drag = { x: event.clientX, y: event.clientY, left: root.scrollLeft, top: root.scrollTop }; root.setPointerCapture(event.pointerId); event.preventDefault(); } }, { signal: controller.signal });
  root.addEventListener('pointermove', event => { if (drag) { root.scrollLeft = drag.left - event.clientX + drag.x; root.scrollTop = drag.top - event.clientY + drag.y; } }, { signal: controller.signal });
  root.addEventListener('pointerup', () => { drag = null; }, { signal: controller.signal });
  root.addEventListener('pointercancel', () => { drag = null; }, { signal: controller.signal });
  const observer = new ResizeObserver(paint); observer.observe(root);
  controller.signal.addEventListener('abort', () => observer.disconnect(), { once: true });
  img.onload = () => { skeleton.remove(); img.hidden = false; paint(); };
  img.onerror = async () => {
    if (controller.signal.aborted) return;
    root.replaceChildren(note('This image could not be displayed. Download the original.'));
    // The image element does not expose HTTP status. Distinguish a removed
    // file from unreadable image bytes before deciding to drop its tab.
    try { await api(contentURL, { signal: controller.signal }); }
    catch (error) { if (error.name !== 'AbortError' && !controller.signal.aborted) onError?.(error); }
  };
  root.append(skeleton, img); content.replaceChildren(root);
  return exposeRoot(content, root);
}
export function renderSource(content, text, language = 'text') {
  text = text.replace(/\r\n?/g, '\n');
  content._disposeRenderer?.(); content._disposeRenderer = null;
  const root = el('pre', { class: 'artifact-source artifact-lines' });
  const lines = text.split('\n');
  const codes = lines.map((line, i) => {
    const code = el('code', { text: line || '\u200b' });
    root.append(el('span', { class: 'artifact-line', 'data-pick': `text:line=${i + 1}` }, [el('span', { class: 'artifact-line-number', text: String(i + 1), 'aria-hidden': 'true' }), code]));
    return code;
  });
  const aliases = { js: 'javascript', ts: 'typescript', py: 'python', md: 'markdown', ps1: 'powershell', sh: 'bash', htm: 'html' };
  language = aliases[language] || language;
  if (text.length < 100000 && hljs.getLanguage(language)) {
    try {
      const fragment = DOMPurify.sanitize(hljs.highlight(text, { language, ignoreIllegals: true }).value, { ALLOWED_TAGS: ['span'], ALLOWED_ATTR: ['class'], RETURN_DOM_FRAGMENT: true });
      codes.forEach(code => code.replaceChildren());
      let line = 0;
      const walk = (node, ancestors = []) => {
        if (node.nodeType === Node.TEXT_NODE) node.textContent.split('\n').forEach((part, i) => {
          if (i) line++;
          if (!codes[line]) return;
          let target = codes[line];
          for (const ancestor of ancestors) { const span = ancestor.cloneNode(false); target.append(span); target = span; }
          target.append(document.createTextNode(part));
        });
        else for (const child of node.childNodes) walk(child, node.nodeType === Node.ELEMENT_NODE ? [...ancestors, node] : ancestors);
      };
      walk(fragment);
    } catch { codes.forEach((code, i) => { code.textContent = lines[i] || '\u200b'; }); }
  }
  content.replaceChildren(root);
  return exposeRoot(content, root);
}
export function renderMarkdown(content, text) {
  content._disposeRenderer?.(); content._disposeRenderer = null;
  const root = el('div', { class: 'artifact-document' });
  const source = text.replace(/\r\n?/g, '\n'), tokens = marked.lexer(source, { gfm: true });
  let line = 1;
  const definitions = tokens.filter(token => token.type === 'def').map(token => token.raw).join('\n');
  for (const token of tokens) {
    const raw = token.raw || '';
    const start = line;
    const end = start + Math.max(0, raw.replace(/\n+$/, '').split('\n').length - 1);
    line += raw.split('\n').length - 1;
    if (token.type === 'space' || token.type === 'def') continue;
    const block = el('div', { 'data-pick': `md:lines=${start}-${end}` });
    renderMessageBody(block, raw + (definitions ? '\n\n' + definitions : ''), null);
    root.append(block);
  }
  content.replaceChildren(root);
  return exposeRoot(content, root);
}
export function renderHTML(content, actions, text, name, controller) {
  content._disposeRenderer?.();
  const root = el('div', { class: 'artifact-html-viewport' });
  const stage = el('div', { class: 'artifact-html-stage' });
  const frame = el('iframe', { sandbox: 'allow-same-origin', title: name, class: 'artifact-frame', referrerpolicy: 'no-referrer' });
  const controls = el('span', { class: 'artifact-html-widths' });
  let width = 1280;
  const resize = () => {
    if (!root.clientWidth) return;
    const available = Math.max(1, root.clientWidth);
    const scale = Math.min(1, available / width);
    frame.style.width = `${width}px`; frame.style.height = `${Math.max(600, root.clientHeight / scale)}px`;
    frame.style.transform = `scale(${scale})`;
    stage.style.width = `${width * scale}px`; stage.style.height = `${Math.max(600 * scale, root.clientHeight)}px`;
  };
  for (const [label, value] of [['Desktop', 1280], ['Tablet', 820], ['Phone', 390]]) {
    const control = button(label, () => { width = value; [...controls.children].forEach(b => b.setAttribute('aria-pressed', String(b === control))); resize(); });
    control.setAttribute('aria-pressed', String(value === width)); controls.append(control);
  }
  const safe = DOMPurify.sanitize(text, { WHOLE_DOCUMENT: true, FORBID_TAGS: ['script', 'iframe', 'object', 'embed', 'form', 'base', 'meta', 'link', 'a'], FORBID_ATTR: ['srcset', 'src', 'href', 'poster', 'background'] });
  frame.srcdoc = `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:; form-action 'none'; base-uri 'none'"><style>body{margin:24px;font-family:system-ui;color:#202124;background:white}</style>${safe}`;
  frame.onload = () => {
    if (controller.signal.aborted) return;
    const body = frame.contentDocument?.body;
    if (!body) return;
    const queue = [[body, 'body']];
    while (queue.length) {
      const [node, path] = queue.pop();
      node.setAttribute('data-pick', `html:path=${path}`);
      const counts = new Map();
      for (const child of node.children) {
        const tag = child.localName, n = (counts.get(tag) || 0) + 1;
        counts.set(tag, n); queue.push([child, `${path}>${tag}:nth-of-type(${n})`]);
      }
    }
    exposeRoot(content, body);
  };
  stage.append(frame); root.append(stage); content.replaceChildren(root); actions.append(controls);
  const observer = new ResizeObserver(resize); observer.observe(root);
  const dispose = () => { observer.disconnect(); controls.remove(); controller.signal.removeEventListener('abort', dispose); };
  content._disposeRenderer = dispose;
  controller.signal.addEventListener('abort', dispose, { once: true });
  resize();
  return exposeRoot(content, root);
}
async function renderTextFile(content, actions, meta, contentURL, controller) {
  const res = await fetch(contentURL, { signal: controller.signal });
  if (!res.ok) throw new Error(`${res.status}: The file could not be loaded`);
  const text = await res.text();
  if (controller.signal.aborted) return;
  const source = () => renderSource(content, text, meta.extension);
  const preview = () => meta.kind === 'html' ? renderHTML(content, actions, text, meta.filename, controller) : meta.kind === 'markdown' ? renderMarkdown(content, text) : source();
  if (meta.kind !== 'text') {
    const previewBtn = button('Preview', () => { preview(); previewBtn.setAttribute('aria-pressed', 'true'); sourceBtn.setAttribute('aria-pressed', 'false'); });
    const sourceBtn = button('Source', () => { source(); previewBtn.setAttribute('aria-pressed', 'false'); sourceBtn.setAttribute('aria-pressed', 'true'); });
    previewBtn.setAttribute('aria-pressed', 'true'); sourceBtn.setAttribute('aria-pressed', 'false'); actions.append(previewBtn, sourceBtn);
  }
  actions.append(button('Copy', () => copyText(text))); return preview();
}
function officeText(data) {
  const rowsText = rows => (rows || []).map(row => row.join('\t')).join('\n');
  if (data.kind === 'xlsx') return (data.sheets || []).map(sheet => rowsText(sheet.rows)).join('\n\n');
  if (data.kind === 'docx') return (data.blocks || []).map(block => block.text || '').join('\n');
  if (data.kind === 'pptx') return (data.slides || []).map(slide =>
    [slide.title, ...(slide.body || []), ...(slide.tables || []).map(rowsText), slide.notes].filter(Boolean).join('\n')).join('\n\n');
  return rowsText(data.rows);
}
export function renderCSV(content, actions, data, contentURL, controller) {
  let request = 0;
  const grid = () => {
    request++;
    const root = officeGrid(data.rows, { sheet: 'csv' });
    content.replaceChildren(root);
    if (data.truncated) content.append(note('Only the first rows are shown. Download for everything.'));
    return exposeRoot(content, root);
  };
  const gridBtn = button('Table', () => { grid(); gridBtn.setAttribute('aria-pressed', 'true'); rawBtn.setAttribute('aria-pressed', 'false'); });
  const rawBtn = button('Source', async () => {
    const ticket = ++request;
    gridBtn.setAttribute('aria-pressed', 'false'); rawBtn.setAttribute('aria-pressed', 'true'); loadingSkeleton(content);
    try {
      const res = await fetch(contentURL, { signal: controller.signal });
      if (!res.ok) throw new Error('The original file could not be loaded.');
      const source = await res.text();
      if (ticket === request && !controller.signal.aborted) renderSource(content, source);
    } catch (error) {
      if (ticket === request && error.name !== 'AbortError' && !controller.signal.aborted) {
        const root = note('The original file could not be loaded.'); content.replaceChildren(root); exposeRoot(content, root);
      }
    }
  });
  gridBtn.setAttribute('aria-pressed', 'true'); rawBtn.setAttribute('aria-pressed', 'false'); actions.append(gridBtn, rawBtn);
  return grid();
}
async function renderOffice(content, actions, query, contentURL, controller) {
  const data = await api(`/api/chat/artifacts/office?${query}`, { signal: controller.signal });
  if (controller.signal.aborted) return;
  content.artifact.kind = data.kind;
  const text = officeText(data);
  if (text) actions.append(button('Copy', () => copyText(text)));
  if (data.kind === 'xlsx') return renderSheets(content, actions, data);
  if (data.kind === 'docx') return renderDoc(content, data);
  if (data.kind === 'pptx') return renderDeck(content, actions, data, controller);
  if (data.kind === 'csv') return renderCSV(content, actions, data, contentURL, controller);
  const root = note('This file has no structured preview.'); content.replaceChildren(root);
  return exposeRoot(content, root);
}

export async function renderArtifact({ sessionId, url, controller, content, actions, detail, onError }) {
  const query = new URLSearchParams({ session_id: sessionId, url }), contentURL = `/api/chat/artifacts/content?${query}`;
  loadingSkeleton(content);
  try {
    const meta = await api(`/api/chat/artifacts?${query}`, { signal: controller.signal });
    if (controller.signal.aborted) return;
    detail.textContent = `${meta.extension.toUpperCase() || 'FILE'} · ${fileSize(meta.size)}`;
    content.artifact = { sessionId, url, ...meta };
    actions.append(el('a', { class: 'btn', text: 'Download', href: `${contentURL}&download=true`, download: meta.filename }));
    if (meta.kind === 'image') renderImage(content, actions, meta, contentURL, controller, onError);
    else if (meta.kind === 'pdf') { const root = await renderPDF(content, actions, contentURL, meta.filename, controller, { artifacts: true }); if (root && !controller.signal.aborted) exposeRoot(content, root); }
    else if (meta.kind === 'office') await renderOffice(content, actions, query, contentURL, controller);
    else if (['text', 'markdown', 'html'].includes(meta.kind)) await renderTextFile(content, actions, meta, contentURL, controller);
    else { const root = el('div', { class: 'artifact-fallback' }, [el('h3', { text: 'Your file is ready' }), el('p', { text: 'This format or file size has no in-app preview. Download the original to open it in its own application.' })]); content.replaceChildren(root); exposeRoot(content, root); }
    return content.viewerRoot;
  } catch (error) {
    if (error.name !== 'AbortError' && !controller.signal.aborted) { onError?.(error); content.textContent = `Preview unavailable. ${error.message}`; exposeRoot(content, content); }
  }
}
