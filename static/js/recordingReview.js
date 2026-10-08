import { api, el, toast } from './api.js';

// Page labels stay text, and the server scans the edited draft before saving.
export function openRecordingReview(recording) {
  let steps = recording.steps;
  let active = true;
  let revision = 0;
  const previousFocus = document.activeElement;
  const name = el('input', { 'aria-label': 'Skill name', required: true });
  const description = el('input', { 'aria-label': 'Skill description' });
  const preview = el('textarea', { rows: '12', 'aria-label': 'SKILL.md preview', spellcheck: false });
  const list = el('ol', { class: 'recording-steps' });
  const status = el('div', { class: 'meta', role: 'status', text: 'Preparing the draft...' });
  const scan = el('div', { class: 'recording-scan' });
  const mention = el('input', { type: 'checkbox' });
  const save = el('button', { type: 'button', class: 'btn primary', text: 'Save', disabled: true });
  const discard = el('button', { type: 'button', class: 'btn', text: 'Discard' });
  const panel = el('section', { class: 'glass modal-panel recording-review', role: 'dialog', 'aria-modal': 'true',
    'aria-label': 'Review recorded skill' }, [
    el('h4', { text: 'Review recorded skill' }),
    el('p', { class: 'meta', text: 'Edit the steps before saving. This skill will be available to every chat and agent. Private text was left in the computer.' }),
    el('div', { class: 'recording-review-body' }, [
      list,
      el('label', { class: 'recording-field' }, [el('span', { text: 'Name' }), name]),
      el('label', { class: 'recording-field' }, [el('span', { text: 'Description' }), description]),
      el('label', { class: 'recording-field' }, [el('span', { text: 'SKILL.md preview' }), preview]),
      ...(recording.agent_id ? [el('label', { class: 'recording-check' }, [mention,
        el('span', { text: `Mention in ${recording.agent_name || 'the agent'}'s instructions` })])] : []),
      scan,
    ]), status, el('div', { class: 'modal-footer' }, [discard, save]),
  ]);
  const backdrop = el('div', { class: 'modal-backdrop recording-backdrop' }, [panel]);
  document.body.append(backdrop);

  function dispose() {
    if (!active) return;
    active = false;
    steps = [];
    preview.value = '';
    document.removeEventListener('keydown', onKey);
    backdrop.remove();
    if (previousFocus?.isConnected) previousFocus.focus();
  }
  function onKey(event) {
    if (event.key === 'Escape') { event.preventDefault(); dispose(); }
    if (event.key === 'Tab') {
      const focusable = [...panel.querySelectorAll('button, input, textarea')].filter(node => !node.disabled);
      const first = focusable[0], last = focusable.at(-1);
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
  }
  document.addEventListener('keydown', onKey);
  discard.onclick = dispose;

  function content() {
    const match = preview.value.match(/^---\n([\s\S]*?)\n---\n([\s\S]*)$/);
    const desc = match?.[1].split('\n').find(line => line.startsWith('description:'))?.slice(12).trim();
    return { name: name.value.trim(), description: desc ?? description.value.trim(), body: match ? match[2].trim() : preview.value,
      ...(recording.agent_id && mention.checked ? { mention_agent_id: recording.agent_id } : {}) };
  }
  const changed = () => { scan.replaceChildren(); status.textContent = ''; };
  name.oninput = preview.oninput = mention.onchange = changed;
  description.oninput = () => {
    const current = content();
    preview.value = `---\ndescription: ${description.value}\n---\n\n${current.body.trim()}\n`;
    changed();
  };
  async function regenerate(first = false) {
    const version = ++revision;
    save.disabled = true;
    scan.replaceChildren();
    try {
      const draft = await api('/api/skills/recording-draft', { method: 'POST',
        body: JSON.stringify({ steps, agent_name: recording.agent_name || null }) });
      if (!active || version !== revision) return;
      steps = draft.steps;
      if (first) { name.value = draft.name; description.value = draft.description; }
      preview.value = `---\ndescription: ${description.value}\n---\n\n${draft.body.trim()}\n`;
      list.replaceChildren(...steps.map((step, index) => {
        const remove = el('button', { type: 'button', class: 'btn quiet', text: 'Remove',
          'aria-label': `Remove step ${index + 1}`, onclick: () => { steps.splice(index, 1); regenerate(); } });
        const row = el('li', {}, [el('div', { class: 'recording-step-row' }, [el('span', { text: draft.labels[index] }), remove])]);
        if (step.kind === 'type' && !step.private) {
          const ask = el('input', { type: 'checkbox', checked: !!step.ask_each_time, onchange: () => {
            step.ask_each_time = ask.checked; regenerate();
          } });
          row.append(el('label', { class: 'recording-check' }, [ask, el('span', { text: 'Ask each time' })]));
        }
        return row;
      }));
      status.textContent = steps.length ? '' : 'No steps remain. Add instructions in the preview before saving.';
      save.disabled = false;
      if (first) name.focus();
    } catch (error) { if (active) status.textContent = error.message; }
  }
  async function saveSkill(confirmed = false, snapshot = content()) {
    if (!snapshot.name) { name.focus(); return; }
    save.disabled = true;
    scan.replaceChildren();
    status.textContent = 'Scanning the skill...';
    try {
      const response = await fetch('/api/skills/from-recording', { method: 'POST',
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ...snapshot, confirmed }) });
      const result = await response.json();
      if (!active) return;
      if (response.ok) { toast(`Saved ${result.slug}`, 'success'); dispose(); return; }
      const detail = result.detail;
      if (response.status === 409 && detail && typeof detail === 'object') {
        status.textContent = detail.needs_confirmation ? 'Review the scan before saving.' : 'This skill was blocked.';
        scan.append(el('pre', { text: detail.report || '' }));
        if (detail.needs_confirmation && JSON.stringify(snapshot) === JSON.stringify(content())) {
          scan.append(el('button', { type: 'button', class: 'btn danger', text: 'Save anyway',
            onclick: () => saveSkill(true, snapshot) }));
        }
      } else status.textContent = typeof detail === 'string' ? detail : 'The skill could not be saved.';
    } catch (error) { if (active) status.textContent = error.message; }
    finally { if (active) save.disabled = false; }
  }
  save.onclick = () => saveSkill();
  regenerate(true);
  return { dispose };
}
