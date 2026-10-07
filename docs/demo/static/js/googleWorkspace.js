import { api, el, toast, confirmDialog } from './api.js';

const BASE = '/api/google';
const MIME = {
  folder: 'application/vnd.google-apps.folder',
  sheet: 'application/vnd.google-apps.spreadsheet',
  form: 'application/vnd.google-apps.form',
};
const kindOf = file => Object.entries(MIME).find(([, mime]) => mime === file.mimeType)?.[0] || 'file';
const action = (path, body) => api(`${BASE}${path}`, { method: 'POST', body: JSON.stringify(body) });
const button = (label, onClick, cls = 'btn') => el('button', { type: 'button', class: cls, text: label,
  onclick: async event => { try { await onClick(event); } catch (error) { toast(error.message || 'Google action failed', 'error'); } },
});

export async function renderGoogleWorkspace(container, header, tabs) {
  container.replaceChildren();
  const body = el('div', { class: 'google-workspace' });
  container.append(el('div', { class: 'view-constrained library-view' }, [header, tabs, body]));
  let status;
  try { status = await api(`${BASE}/status`); }
  catch (error) {
    body.append(el('div', { class: 'library-empty', text: error.message.startsWith('403:')
      ? 'Only an admin can connect and manage a Google account.' : 'Google Workspace is unavailable right now.' }));
    return;
  }
  if (!status.configured || !status.connected) { renderConnection(body, status, () => renderGoogleWorkspace(container, header, tabs)); return; }
  renderDrive(body, status, () => renderGoogleWorkspace(container, header, tabs));
}

function renderConnection(host, status, refresh) {
  host.replaceChildren();
  const onHost = Boolean(window.jarvis?.openGoogleSignIn)
    || ['127.0.0.1', 'localhost', '::1'].includes(window.location.hostname);
  const client = el('input', { type: 'text', value: status.client_id || '',
    placeholder: 'Google OAuth Desktop client ID', 'aria-label': 'Google OAuth Desktop client ID' });
  const secret = el('input', { type: 'password', placeholder: 'Client secret, if provided',
    'aria-label': 'Google OAuth client secret' });
  const note = el('p', { class: 'meta', text: 'Create a Desktop OAuth client in Google Cloud with Drive, Sheets, and Forms APIs enabled. Add your Google account as a test user if the consent app is in Testing. Sign in on the computer running Kairos; connected files are then available from other devices.' });
  const save = button('Save client ID', async () => {
    await action('/client', { client_id: client.value.trim(), client_secret: secret.value });
    toast('Google client saved', 'success');
    refresh();
  }, 'btn primary');
  const connect = button('Connect Google account', async () => {
    const result = await action('/oauth/start', {});
    if (window.jarvis?.openGoogleSignIn) {
      if (!await window.jarvis.openGoogleSignIn(result.url)) throw new Error('Could not open Google sign-in');
    } else window.open(result.url, '_blank', 'noopener');
    connect.disabled = true;
    connect.textContent = 'Waiting for Google…';
    const started = Date.now();
    const poll = async () => {
      if (!host.isConnected || Date.now() - started > 10 * 60 * 1000) { connect.disabled = false; connect.textContent = 'Connect Google account'; return; }
      try { if ((await api(`${BASE}/status`)).connected) { refresh(); return; } } catch { /* retry */ }
      setTimeout(poll, 1800);
    };
    poll();
  }, 'btn primary');
  connect.disabled = !onHost;
  host.append(el('div', { class: 'glass card google-connect' }, [
    el('div', { class: 'title', text: status.configured ? 'Connect Google Workspace' : 'Set up Google Workspace' }),
    note,
    el('div', { class: 'google-field-row' }, [client, secret, save]),
    el('div', { class: 'google-field-row' }, [
      status.configured ? connect : el('span', { class: 'meta', text: 'Save a client ID to continue.' }),
      !onHost ? el('span', { class: 'meta', text: 'Open Library on the Kairos host computer to connect.' }) : null,
      el('a', { href: 'https://console.cloud.google.com/apis/credentials', target: '_blank', rel: 'noopener noreferrer', text: 'Google Cloud credentials ↗' }),
    ]),
  ]));
}

function renderDrive(host, status, refreshAll) {
  host.replaceChildren();
  const state = { parent: null, trail: [], kind: 'all', query: '', trashed: false, page: null, selected: null };
  const search = el('input', { type: 'search', placeholder: 'Search Google Drive', 'aria-label': 'Search Google Drive' });
  const kind = el('select', { 'aria-label': 'File type' }, [
    ...[['all', 'All files'], ['folder', 'Folders'], ['sheet', 'Sheets'], ['form', 'Forms']]
      .map(([value, label]) => el('option', { value, text: label })),
  ]);
  const trash = el('input', { type: 'checkbox' });
  const list = el('div', { class: 'google-file-list' });
  const detail = el('div', { class: 'google-detail' });
  const breadcrumbs = el('div', { class: 'google-breadcrumbs' });
  const statusLine = el('div', { class: 'meta', role: 'status' });
  let listGeneration = 0;
  const more = button('Load more', () => load(true));
  more.hidden = true;
  const uploadInput = el('input', { type: 'file', hidden: true });
  const toolbar = el('div', { class: 'google-toolbar' }, [
    search, kind, el('label', { class: 'google-trash-toggle' }, [trash, el('span', { text: 'Trash' })]),
    button('Refresh', () => load()),
    button('Upload', () => uploadInput.click()), uploadInput,
    button('New folder', async () => {
      const name = await inlinePrompt(detail, 'New folder name');
      if (name) await driveAction({ action: 'create_folder', name, parent: state.parent });
    }),
    button('New Sheet', async () => {
      const title = await inlinePrompt(detail, 'Spreadsheet title');
      if (title) {
        const created = await action('/sheets/action', { action: 'create', title });
        if (state.parent && created.spreadsheetId) await action('/drive/action', { action: 'move', file_id: created.spreadsheetId, parent: state.parent });
        await load();
      }
    }),
    button('New Form', async () => {
      const title = await inlinePrompt(detail, 'Form title');
      if (title) {
        const created = await action('/forms/action', { action: 'create', title });
        if (state.parent && created.formId) await action('/drive/action', { action: 'move', file_id: created.formId, parent: state.parent });
        await load();
      }
    }),
  ]);
  const disconnect = button('Disconnect', async () => {
    const yes = await confirmDialog({ title: 'Disconnect Google?', message: 'Kairos will forget this account’s tokens. Files stay in Google Drive.', confirmLabel: 'Disconnect' });
    if (yes) { await api(`${BASE}/connection`, { method: 'DELETE' }); refreshAll(); }
  }, 'btn quiet');
  host.append(
    el('div', { class: 'google-account-line' }, [
      el('div', {}, [el('strong', { text: status.email || 'Google account' }),
        el('div', { class: 'meta', text: 'Drive · Sheets · Forms' })]), disconnect,
    ]), toolbar, breadcrumbs, statusLine,
    el('div', { class: 'google-browser' }, [list, detail]), more,
  );
  search.addEventListener('input', () => { state.query = search.value.trim(); state.parent = null; state.trail = []; schedule(); });
  kind.addEventListener('change', () => { state.kind = kind.value; load(); });
  trash.addEventListener('change', () => { state.trashed = trash.checked; load(); });
  uploadInput.addEventListener('change', async () => {
    const file = uploadInput.files?.[0]; uploadInput.value = '';
    if (!file) return;
    const data = new FormData(); data.append('file', file); if (state.parent) data.append('parent', state.parent);
    const response = await fetch(`${BASE}/drive/upload`, { method: 'POST', body: data });
    if (!response.ok) { const payload = await response.json().catch(() => ({})); toast(payload.detail || 'Upload failed', 'error'); return; }
    toast(`${file.name} uploaded`, 'success'); await load();
  });
  let timer;
  function schedule() { clearTimeout(timer); timer = setTimeout(() => load(), 250); }
  async function driveAction(body) { const result = await action('/drive/action', body); await load(); return result; }
  function openFolder(file) {
    state.trail.push({ id: file.id, name: file.name }); state.parent = file.id; state.query = ''; search.value = '';
    load();
  }
  function drawBreadcrumbs() {
    breadcrumbs.replaceChildren(button('All files', () => { state.parent = null; state.trail = []; load(); }, 'btn quiet'));
    state.trail.forEach((item, index) => breadcrumbs.append(
      el('span', { class: 'meta', text: '›' }),
      button(item.name, () => { state.trail = state.trail.slice(0, index + 1); state.parent = item.id; load(); }, 'btn quiet'),
    ));
  }
  async function load(append = false) {
    const generation = ++listGeneration;
    const params = new URLSearchParams({ q: state.query, kind: state.kind, trashed: String(state.trashed) });
    if (state.parent) params.set('parent', state.parent);
    if (append && state.page) params.set('page_token', state.page);
    statusLine.textContent = 'Loading Drive…';
    try {
      const result = await api(`${BASE}/drive/files?${params}`);
      if (generation !== listGeneration || !host.isConnected) return;
      if (!append) list.replaceChildren();
      state.page = result.nextPageToken || null;
      more.hidden = !state.page;
      for (const file of result.files || []) {
        const type = kindOf(file);
        const row = button(file.name, () => type === 'folder' ? openFolder(file) : showDetail(file), 'google-file-row');
        row.replaceChildren(
          el('span', { class: `google-type google-type-${type}`, text: type === 'file' ? 'FILE' : type.toUpperCase() }),
          el('span', { class: 'google-file-copy' }, [el('strong', { text: file.name }),
            el('small', { text: [type, file.modifiedTime && new Date(file.modifiedTime).toLocaleDateString()].filter(Boolean).join(' · ') })]),
          el('span', { class: 'library-row-arrow', text: '→' }),
        );
        list.append(row);
      }
      if (!list.childElementCount) list.append(el('div', { class: 'library-empty', text: 'No matching Google files.' }));
      statusLine.textContent = result.incompleteSearch ? 'Google returned a partial result. Narrow the search.' : '';
      drawBreadcrumbs();
    } catch (error) { statusLine.textContent = `Could not load Drive: ${error.message}`; }
  }
  async function showDetail(file) {
    state.selected = file;
    detail.replaceChildren();
    const type = kindOf(file);
    const head = el('div', { class: 'google-detail-head' }, [
      el('div', {}, [el('div', { class: 'meta', text: type.toUpperCase() }), el('h3', { text: file.name })]),
      file.webViewLink ? el('a', { class: 'btn', href: file.webViewLink, target: '_blank', rel: 'noopener noreferrer', text: 'Open in Google ↗' }) : null,
    ]);
    const actions = el('div', { class: 'google-actions' });
    const apply = async (body) => { await driveAction({ file_id: file.id, ...body }); detail.replaceChildren(); };
    actions.append(
      button('Rename', async () => { const name = await inlinePrompt(detail, 'New name', file.name); if (name) await apply({ action: 'rename', name }); }),
      button('Move', async () => { const parent = await folderPicker(detail); if (parent) await apply({ action: 'move', parent }); }),
      button('Copy', async () => apply({ action: 'copy', name: `${file.name} copy` })),
      button(file.starred ? 'Unstar' : 'Star', async () => apply({ action: file.starred ? 'unstar' : 'star' })),
      button(file.trashed ? 'Restore' : 'Move to trash', async () => {
        if (file.trashed || await confirmDialog({ title: 'Move to trash?', message: file.name, confirmLabel: 'Move to trash' }))
          await apply({ action: file.trashed ? 'restore' : 'trash' });
      }),
      file.trashed ? button('Delete forever', async () => {
        if (await confirmDialog({ title: 'Delete permanently?', message: `${file.name} cannot be restored.`, confirmLabel: 'Delete forever' })) await apply({ action: 'delete' });
      }, 'btn danger') : null,
    );
    if (type === 'file' || type === 'sheet') actions.append(el('a', { class: 'btn', href: `${BASE}/drive/files/${encodeURIComponent(file.id)}/download`, text: 'Download' }));
    const extras = el('div', { class: 'google-detail-extra' });
    detail.append(head, actions, extras);
    if (type === 'sheet') await sheetEditor(extras, file);
    if (type === 'form') await formEditor(extras, file);
    const permissions = el('details', { class: 'disclosure-panel' }, [el('summary', { text: 'Sharing and permissions' })]);
    permissions.addEventListener('toggle', () => { if (permissions.open && permissions.childElementCount === 1) sharing(permissions, file); });
    const revisions = el('details', { class: 'disclosure-panel' }, [el('summary', { text: 'Revisions' })]);
    revisions.addEventListener('toggle', async () => {
      if (!revisions.open || revisions.childElementCount > 1) return;
      try { const r = await action('/drive/action', { action: 'revisions', file_id: file.id });
        revisions.append(...(r.revisions || []).map(v => el('div', { class: 'meta', text: new Date(v.modifiedTime).toLocaleString() }))); }
      catch { revisions.append(el('div', { class: 'meta', text: 'Revisions unavailable for this file.' })); }
    });
    detail.append(permissions, revisions);
  }
  load();
}

function inlinePrompt(host, label, initial = '') {
  return new Promise(resolve => {
    const input = el('input', { value: initial, 'aria-label': label });
    const panel = el('div', { class: 'google-inline-prompt' }, [
      el('label', { text: label }), input,
      button('Save', () => done(input.value.trim()), 'btn primary'),
      button('Cancel', () => done(null), 'btn quiet'),
    ]);
    const done = value => { panel.remove(); resolve(value); };
    host.prepend(panel); input.focus(); input.select();
    input.addEventListener('keydown', e => { if (e.key === 'Enter') done(input.value.trim()); if (e.key === 'Escape') done(null); });
  });
}

function folderPicker(host) {
  return new Promise(resolve => {
    const query = el('input', { type: 'search', placeholder: 'Find a destination folder', 'aria-label': 'Find a destination folder' });
    const results = el('div', { class: 'google-folder-results' });
    const next = button('More folders', () => load(true)); next.hidden = true;
    const panel = el('div', { class: 'google-inline-prompt' }, [
      el('strong', { text: 'Move to folder' }), query,
      button('My Drive root', () => done('root'), 'btn quiet'), results, next,
      button('Cancel', () => done(null), 'btn quiet'),
    ]);
    let page = null;
    let generation = 0;
    const done = value => { panel.remove(); resolve(value); };
    async function load(append = false) {
      const own = ++generation;
      const params = new URLSearchParams({ q: query.value.trim(), kind: 'folder' });
      if (append && page) params.set('page_token', page);
      if (!append) results.textContent = 'Loading folders…';
      try {
        const data = await api(`${BASE}/drive/files?${params}`);
        if (own !== generation || !panel.isConnected) return;
        if (!append) results.replaceChildren();
        page = data.nextPageToken || null; next.hidden = !page;
        results.append(...(data.files || []).map(folder => button(folder.name, () => done(folder.id), 'google-file-row')));
        if (!results.childElementCount) results.textContent = 'No matching folders.';
      } catch (error) { results.textContent = `Could not load folders: ${error.message}`; }
    }
    host.prepend(panel); query.focus();
    let timer;
    query.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(() => load(), 250); });
    load();
  });
}

async function sharing(host, file) {
  const members = el('div', { class: 'google-members' });
  const audience = el('select', { 'aria-label': 'Share with' }, [
    ...[['user', 'Person'], ['group', 'Group'], ['domain', 'Domain'], ['anyone', 'Anyone with the link']]
      .map(([value, label]) => el('option', { value, text: label })),
  ]);
  const email = el('input', { type: 'text', placeholder: 'Email address', 'aria-label': 'Email address or domain' });
  audience.addEventListener('change', () => {
    email.hidden = audience.value === 'anyone';
    email.placeholder = audience.value === 'domain' ? 'example.com' : 'Email address';
    email.value = '';
  });
  const role = el('select', { 'aria-label': 'Access level' }, [
    ...['reader', 'commenter', 'writer'].map(value => el('option', { value, text: value })),
  ]);
  const load = async () => {
    const result = await action('/drive/action', { action: 'permissions', file_id: file.id });
    members.replaceChildren(...(result.permissions || []).map(p => el('div', { class: 'google-member' }, [
      el('span', { text: p.emailAddress || p.domain || p.displayName || p.type }),
      p.role === 'owner' ? el('span', { class: 'meta', text: 'Owner' }) : el('select', { 'aria-label': `Access for ${p.emailAddress || p.domain || p.type}` }, [
        ...['reader', 'commenter', 'writer'].map(value => el('option', { value, text: value, selected: value === p.role })),
      ]),
      p.role === 'owner' ? null : button('Update', async e => {
        const value = e.currentTarget.previousElementSibling.value;
        await action('/drive/action', { action: 'update_permission', file_id: file.id, permission_id: p.id, role: value }); await load();
      }, 'btn quiet'),
      p.role === 'owner' ? null : button('Remove', async () => {
        if (!await confirmDialog({ title: 'Remove access?', message: p.emailAddress || p.displayName || 'This person', confirmLabel: 'Remove' })) return;
        await action('/drive/action', { action: 'revoke', file_id: file.id, permission_id: p.id }); await load();
      }, 'btn quiet'),
    ])));
  };
  host.append(el('div', { class: 'google-share-form' }, [audience, email, role, button('Share', async () => {
    await action('/drive/action', { action: 'share', file_id: file.id,
      permission_type: audience.value, email: audience.value === 'domain' ? null : email.value.trim(),
      domain: audience.value === 'domain' ? email.value.trim() : null, role: role.value });
    email.value = ''; await load();
  }, 'btn primary')]), members);
  try { await load(); } catch (error) { members.textContent = `Could not load sharing: ${error.message}`; }
}

async function sheetEditor(host, file) {
  let meta;
  try { meta = await api(`${BASE}/sheets/${encodeURIComponent(file.id)}`); }
  catch (error) { host.textContent = `Could not open spreadsheet: ${error.message}`; return; }
  const sheets = meta.sheets || [];
  const tabs = el('div', { class: 'segmented-tabs' });
  const grid = el('div', { class: 'google-sheet-grid' });
  const range = el('input', { value: 'A1:L20', 'aria-label': 'Cell range' });
  const status = el('span', { class: 'meta' });
  let sheet = sheets[0];
  let cells = [];
  let loadedRange = '';
  const dimensions = () => {
    const match = /^([A-Z]+)([1-9]\d*):([A-Z]+)([1-9]\d*)$/i.exec(range.value.trim());
    if (!match) throw new Error('Use a range such as A1:L20');
    const column = letters => [...letters.toUpperCase()].reduce((n, char) => n * 26 + char.charCodeAt(0) - 64, 0);
    const rows = Number(match[4]) - Number(match[2]) + 1;
    const cols = column(match[3]) - column(match[1]) + 1;
    if (rows < 1 || rows > 20 || cols < 1 || cols > 12) throw new Error('Choose up to 20 rows and 12 columns');
    return { rows, cols, startRow: Number(match[2]), startCol: column(match[1]) };
  };
  const drawTabs = () => { tabs.replaceChildren(...sheets.map(item => button(item.properties.title, () => {
    sheet = item; drawTabs(); load();
  }, item === sheet ? 'segmented-tab active' : 'segmented-tab'))); };
  const load = async () => {
    if (!sheet) return;
    status.textContent = 'Loading cells…';
    cells = []; loadedRange = '';
    try {
    const shape = dimensions();
    const data = await api(`${BASE}/sheets/${encodeURIComponent(file.id)}?cell_range=${encodeURIComponent(`'${sheet.properties.title.replaceAll("'", "''")}'!${range.value}`)}`);
    grid.style.setProperty('--google-sheet-columns', String(shape.cols));
    grid.replaceChildren(); cells = [];
    for (let r = 0; r < shape.rows; r++) {
      const row = [];
      for (let c = 0; c < shape.cols; c++) {
        const input = el('input', { value: data.values?.[r]?.[c] ?? '', 'aria-label': `Row ${shape.startRow + r} column ${shape.startCol + c}` });
        row.push(input); grid.append(input);
      }
      cells.push(row);
    }
    loadedRange = range.value.trim();
    status.textContent = '';
    } catch (error) { status.textContent = error.message; }
  };
  host.append(el('div', { class: 'google-editor-head' }, [el('strong', { text: 'Spreadsheet' }), tabs]),
    el('div', { class: 'google-field-row' }, [range, button('Load range', load), button('Save cells', async () => {
      try { dimensions(); } catch (error) { status.textContent = error.message; return; }
      if (!cells.length) { status.textContent = 'Load a range first'; return; }
      if (range.value.trim() !== loadedRange) { status.textContent = 'Load the new range before saving'; return; }
      const cellRange = `'${sheet.properties.title.replaceAll("'", "''")}'!${range.value}`;
      await action('/sheets/action', { action: 'update', file_id: file.id, cell_range: cellRange,
        values: cells.map(row => row.map(input => input.value)) });
      toast('Sheet saved', 'success');
    }, 'btn primary'), status]), grid);
  drawTabs(); await load();
}

async function formEditor(host, file) {
  const form = await api(`${BASE}/forms/${encodeURIComponent(file.id)}`);
  const list = el('div', { class: 'google-form-items' });
  const responses = el('div', { class: 'google-form-responses' });
  const title = el('input', { value: form.info?.title || file.name, 'aria-label': 'Form title' });
  const question = el('input', { placeholder: 'Question text', 'aria-label': 'New question' });
  const type = el('select', { 'aria-label': 'Question type' }, [
    el('option', { value: 'TEXT', text: 'Short answer' }),
    el('option', { value: 'PARAGRAPH_TEXT', text: 'Paragraph' }),
    el('option', { value: 'RADIO', text: 'Multiple choice' }),
    el('option', { value: 'CHECKBOX', text: 'Checkboxes' }),
  ]);
  const options = el('input', { placeholder: 'Choices, comma separated', 'aria-label': 'Choices' });
  const drawItems = items => {
    list.replaceChildren(...(items || []).map((item, index) => el('div', { class: 'google-form-item' }, [
      el('span', { text: item.title || `Question ${index + 1}` }),
      button('Remove', async () => {
        if (!await confirmDialog({ title: 'Remove question?', message: item.title || 'This question', confirmLabel: 'Remove' })) return;
        await action('/forms/action', { action: 'batch', file_id: file.id,
          requests: [{ deleteItem: { location: { index } } }] });
        form.items = (await api(`${BASE}/forms/${encodeURIComponent(file.id)}`)).items;
        drawItems(form.items);
      }, 'btn quiet'),
    ])));
  };
  drawItems(form.items);
  let isPublished = Boolean(form.publishSettings?.publishState?.isPublished);
  host.append(
    el('div', { class: 'google-editor-head' }, [el('strong', { text: 'Form' }),
      button(isPublished ? 'Unpublish' : 'Publish', async e => {
        await action('/forms/action', { action: 'publish', file_id: file.id, published: !isPublished });
        isPublished = !isPublished;
        e.currentTarget.textContent = isPublished ? 'Unpublish' : 'Publish'; toast('Publish state updated', 'success');
      })]),
    el('div', { class: 'google-field-row' }, [title, button('Save title', async () => {
      await action('/forms/action', { action: 'batch', file_id: file.id,
        requests: [{ updateFormInfo: { info: { title: title.value }, updateMask: 'title' } }] });
      toast('Form title saved', 'success');
    })]), list,
    el('div', { class: 'google-field-row' }, [question, type, options, button('Add question', async () => {
      const text = question.value.trim(); if (!text) return;
      const choiceQuestion = ['RADIO', 'CHECKBOX'].includes(type.value);
      if (choiceQuestion && !options.value.split(',').some(value => value.trim())) {
        toast('Add at least one choice', 'error'); return;
      }
      const q = choiceQuestion
        ? { choiceQuestion: { type: type.value, options: options.value.split(',').map(v => v.trim()).filter(Boolean).map(value => ({ value })) } }
        : { textQuestion: { paragraph: type.value === 'PARAGRAPH_TEXT' } };
      await action('/forms/action', { action: 'batch', file_id: file.id,
        requests: [{ createItem: { item: { title: text, questionItem: { question: { required: false, ...q } } }, location: { index: (form.items || []).length } } }] });
      question.value = ''; options.value = '';
      const updated = await api(`${BASE}/forms/${encodeURIComponent(file.id)}`);
      form.items = updated.items; drawItems(updated.items);
    }, 'btn primary')]),
    button('Load responses', async () => {
      const result = await api(`${BASE}/forms/${encodeURIComponent(file.id)}?responses=true`);
      responses.replaceChildren(...(result.responses || []).map(item => el('pre', { text: JSON.stringify(item.answers || {}, null, 2) })));
      if (!responses.childElementCount) responses.textContent = 'No responses yet.';
    }), responses,
  );
}
