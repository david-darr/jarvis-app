// One view for chat and agent computers. The stream exists only while mounted.
import { api, el, toast } from './api.js';
import { openRecordingReview } from './recordingReview.js';

export function mountComputerPanel(host, owner, initial = {}, { onClose, takeOver = false } = {}) {
  const endpoint = `/api/computer/${encodeURIComponent(owner)}`;
  let active = true;
  let control = !!initial.taken_over;
  let desktop = !!initial.desktop;
  let closed = !!initial.closed_at;
  let stream = null;
  let retry = null;
  let recording = !!initial.recording;
  let review = null;
  const title = el('div', { class: 'computer-title', text: initial.title || 'Contained computer' });
  const url = el('div', { class: 'computer-url', text: initial.url || 'about:blank' });
  const time = el('div', { class: 'meta computer-time' });
  const state = el('div', { class: 'computer-state', role: 'status' });
  const frame = el('img', { class: 'computer-frame', alt: 'Live contained computer', draggable: false });
  const stage = el('div', { class: 'computer-stage', tabindex: '0', role: 'application',
    'aria-label': 'Contained computer. Take over to use the keyboard.' }, [frame]);
  const stop = el('button', { type: 'button', class: 'btn danger', text: 'Stop' });
  const toggle = el('button', { type: 'button', class: 'btn', text: control ? 'Hand back' : 'Take over' });
  const record = el('button', { type: 'button', class: 'btn computer-record', text: 'Record', hidden: !control });
  const recordingState = el('span', { class: 'computer-recording-indicator', role: 'status', text: 'Recording', hidden: !recording });
  const root = el('section', { class: 'computer-panel' }, [
    el('div', { class: 'computer-panel-head' }, [title, el('div', { class: 'computer-actions' }, [toggle, stop, record, recordingState])]),
    url, time, state, stage,
  ]);
  host.replaceChildren(root);

  function render(info = {}) {
    const wasClosed = closed;
    if (info.closed_at === null) closed = false;
    if (info.closed_at) closed = true;
    if (info.title != null) title.textContent = info.title || 'Contained computer';
    if (info.url != null) url.textContent = info.url;
    if (info.last_action) time.textContent = `Last action ${new Date(info.last_action * 1000).toLocaleTimeString()}`;
    if (info.taken_over != null) control = !!info.taken_over;
    if (info.desktop != null) desktop = !!info.desktop;
    if (info.recording != null) recording = !!info.recording;
    if (closed) control = false;
    if (closed || !control) { recording = false; review?.dispose(); review = null; }
    root.classList.toggle('is-recording', recording);
    recordingState.hidden = !recording;
    record.hidden = !control || closed;
    record.textContent = recording ? 'Stop recording' : 'Record';
    root.classList.toggle('is-taken-over', control);
    toggle.textContent = control ? 'Hand back' : 'Take over';
    state.textContent = closed ? 'Computer closed' : control ? (info.waiting_model ? 'You have control. The model is waiting.' : 'You have control.')
      : 'The model has control.';
    if (control && desktop) state.textContent += ' Ctrl+Esc opens desktop apps.';
    toggle.disabled = closed;
    stop.disabled = closed;
    if (info.image) frame.src = `data:image/jpeg;base64,${info.image}`;
    else if (info.image_url) frame.src = info.image_url;
    if (closed) stream?.close();
    else if (wasClosed) connect();
  }
  render(initial);

  async function retainLast() {
    const saved = await api(`${endpoint}/last`).catch(() => null);
    if (active && saved) { closed = true; render(saved); return true; }
    return false;
  }
  function connect() {
    if (!active || closed) return;
    stream = new EventSource(`${endpoint}/frames`);
    stream.addEventListener('frame', event => { if (active) render(JSON.parse(event.data)); });
    stream.addEventListener('closed', async () => {
      stream.close();
      if (active) { closed = true; render({}); await retainLast(); }
    });
    stream.addEventListener('error', async () => {
      stream.close();
      if (active && !await retainLast() && !closed) retry = setTimeout(connect, 1000);
    });
  }
  connect();

  // One input at a time, in order: sent all at once, quick typing can arrive
  // shuffled ("kairos" became "kaiors"). A failed input doesn't stop the queue.
  let inputs = Promise.resolve();
  const send = (kind, values) => {
    const sent = inputs.then(() => api(`${endpoint}/input`, { method: 'POST', body: JSON.stringify({ kind, ...values }) }));
    inputs = sent.catch(() => {});
    return sent;
  };
  stage.addEventListener('click', async event => {
    if (!control || !frame.src) return;
    stage.focus();
    // The frame is letterboxed (object-fit: contain), so map from the drawn
    // picture, not the element box; a click in the bars is ignored.
    const rect = frame.getBoundingClientRect();
    const scale = Math.min(rect.width / 1280, rect.height / 800);
    const left = rect.left + (rect.width - 1280 * scale) / 2;
    const top = rect.top + (rect.height - 800 * scale) / 2;
    // + 1e-6: float division can land a pixel boundary at 639.9999.
    const x = Math.floor((event.clientX - left) / scale + 1e-6);
    const y = Math.floor((event.clientY - top) / scale + 1e-6);
    if (x < 0 || x > 1279 || y < 0 || y > 799) return;
    try { await send('click', { x, y }); } catch (error) { toast(error.message, 'error'); }
  });
  stage.addEventListener('keydown', async event => {
    if (!control) return;
    const special = ['Enter', 'Tab', 'Backspace', 'Delete', 'Escape', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Home', 'End'];
    let kind, values;
    if (event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey) {
      kind = 'type'; values = { text: event.key };
    } else if (special.includes(event.key) || event.ctrlKey || event.metaKey) {
      kind = 'key'; values = { key: `${event.ctrlKey ? 'Control+' : ''}${event.metaKey ? 'Meta+' : ''}${event.shiftKey ? 'Shift+' : ''}${event.key}` };
    } else return;
    event.preventDefault();
    try { await send(kind, values); } catch (error) { toast(error.message, 'error'); }
  });
  stage.addEventListener('wheel', async event => {
    if (!control) return;
    event.preventDefault();
    try { await send('scroll', { dx: Math.round(event.deltaX), dy: Math.round(event.deltaY) }); }
    catch (error) { toast(error.message, 'error'); }
  }, { passive: false });
  toggle.onclick = async () => {
    toggle.disabled = true;
    try {
      await api(`${endpoint}/${control ? 'handback' : 'takeover'}`, { method: 'POST' });
      render({ taken_over: !control });
      if (control) stage.focus();
    } finally { toggle.disabled = false; }
  };
  stop.onclick = async () => {
    stop.disabled = true;
    try { await api(`${endpoint}/stop`, { method: 'POST' }); stream?.close(); closed = true; render({}); await retainLast(); }
    catch { stop.disabled = false; }
  };
  record.onclick = async () => {
    record.disabled = true;
    try {
      if (recording) {
        const result = await api(`${endpoint}/record/stop`, { method: 'POST' });
        render({ recording: false });
        review?.dispose();
        if (active && control && !closed) review = openRecordingReview(result);
      } else {
        review?.dispose(); review = null;
        await api(`${endpoint}/record/start`, { method: 'POST' });
        if (!active || !control || closed) {
          await api(`${endpoint}/record/stop`, { method: 'POST' }).catch(() => {});
          return;
        }
        render({ recording: true });
        stage.focus();
      }
    } finally { record.disabled = false; }
  };
  if (takeOver && !control && !closed) toggle.click();

  function dispose() {
    if (recording) api(`${endpoint}/record/stop`, { method: 'POST' }).catch(() => {});
    review?.dispose(); review = null;
    active = false;
    stream?.close();
    clearTimeout(retry);
    root.remove();
  }
  return { root, dispose, update: render };
}
