import { api, el, confirmDialog } from './api.js';
import { highlightCode } from './chatContent.js';
import { forgeRunning } from './forgeUi.js';

const languages = { js: 'javascript', cjs: 'javascript', mjs: 'javascript', ts: 'typescript', tsx: 'typescript', jsx: 'javascript', py: 'python', json: 'json', html: 'xml', svg: 'xml', css: 'css', md: 'markdown', yml: 'yaml', yaml: 'yaml', sh: 'bash', ps1: 'powershell', sql: 'sql', rs: 'rust', go: 'go', java: 'java', c: 'c', h: 'c', cpp: 'cpp', toml: 'ini' };

export function fileView(file) {
  const code = el('code'); highlightCode(code, file.content, languages[file.path.split('.').pop().toLowerCase()] || 'text');
  const numbers = el('div', { class: 'forge-line-numbers', 'aria-hidden': 'true', text: file.content.split('\n').map((_, i) => i + 1).join('\n') });
  return el('div', { class: 'forge-file-viewer' }, [
    el('div', { class: 'forge-file-title', text: `${file.path} · Read only` }),
    el('div', { class: 'forge-code-scroll', tabindex: '0', 'aria-label': `Contents of ${file.path}` }, [el('div', { class: 'forge-code-lines' }, [numbers, el('pre', {}, [code])])]),
  ]);
}
export function changeRole(file) {
  if (file.untracked) return 'U';
  if (/^deleted file mode /m.test(file.patch || '') || file.status === 'deleted') return 'D';
  if (/^new file mode /m.test(file.patch || '') || file.status === 'added') return 'A';
  return 'M';
}
export function diffView(file, { onRevertHunk, disabled = false } = {}) {
  const content = el('div', { class: 'forge-diff-scroll', tabindex: '0', 'aria-label': `Diff of ${file.path}` });
  const header = file.patch || '';
  // Fixtures and unavailable patch readers can supply header and hunks separately.
  const patch = header.includes('@@ ') ? header : header + (file.hunks || []).map(h => h.patch).join('');
  let oldLine = null, newLine = null, hunkIndex = 0;
  for (const line of patch.split('\n')) {
    const hunk = line.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
    let oldNumber = '', newNumber = '', role = 'context', marker = '', text = line;
    if (hunk) { oldLine = Number(hunk[1]); newLine = Number(hunk[2]); role = 'hunk'; }
    else if (oldLine !== null && line.startsWith('+')) { newNumber = newLine++; role = 'add'; marker = '+'; text = line.slice(1); }
    else if (oldLine !== null && line.startsWith('-')) { oldNumber = oldLine++; role = 'remove'; marker = '-'; text = line.slice(1); }
    else if (oldLine !== null && line.startsWith(' ')) { oldNumber = oldLine++; newNumber = newLine++; text = line.slice(1); }
    const row = el('div', { class: `forge-diff-line diff-${role}` }, [el('span', { class: 'forge-diff-number', text: String(oldNumber) }), el('span', { class: 'forge-diff-number', text: String(newNumber) }), el('span', { class: 'forge-diff-marker', text: marker }), el('code', { text })]);
    if (hunk) {
      const reviewed = file.hunks?.[hunkIndex++];
      // Use the server's hash only when its hunk matches this displayed header.
      if (onRevertHunk && reviewed?.hash && reviewed.patch.split('\n')[0] === line) row.append(el('button', {
        type: 'button', class: 'forge-small-button forge-revert-hunk', text: 'Revert hunk',
        'aria-label': `Revert this change in ${file.path}`, 'data-hunk-hash': reviewed.hash,
        disabled, onclick: () => onRevertHunk(reviewed),
      }));
    }
    content.append(row);
  }
  if (file.unavailable || file.binary || !patch.trim()) content.replaceChildren(el('p', { class: 'meta', text: file.unavailable || (file.binary ? 'Binary file. Text diff is unavailable.' : 'No changes from the session baseline.') }));
  return el('div', { class: 'forge-diff-viewer' }, [el('div', { class: 'forge-file-title', text: `${file.path} · Unified diff` }), content]);
}
export async function revertFile(id, file, blocked = () => false) {
  if (forgeRunning(id) || file.unavailable || blocked()) return false;
  if (!await confirmDialog({ title: 'Revert file?', message: `Restore ${file.path} to the session baseline? This discards all changes to this file, including changes made outside the agent.`, confirmLabel: 'Revert file' })) return false;
  if (forgeRunning(id) || blocked()) return false;
  await api(`/api/forge/sessions/${encodeURIComponent(id)}/revert-file`, { method: 'POST', body: JSON.stringify({ path: file.path, confirmed: true }) });
  return true;
}
export async function revertHunk(id, file, hunk, blocked = () => false) {
  if (forgeRunning(id) || file.unavailable || !hunk.hash || blocked()) return false;
  // A deletion has no current lines, so name its range in the baseline instead.
  const start = hunk.new_count ? hunk.new_start : hunk.old_start;
  const count = hunk.new_count || hunk.old_count;
  const range = count > 1 ? `lines ${start}-${start + count - 1}` : `line ${start}`;
  if (!await confirmDialog({ title: 'Revert hunk?', message: `Revert this change in ${file.path}, ${range}${hunk.new_count ? '' : ' in the session baseline'}? This discards the changes in this hunk, including changes made outside the agent.`, confirmLabel: 'Revert hunk' })) return false;
  if (forgeRunning(id) || blocked()) return false;
  await api(`/api/forge/sessions/${encodeURIComponent(id)}/revert-hunk`, { method: 'POST', toast: false, body: JSON.stringify({ path: file.path, hunk_hash: hunk.hash, confirmed: true }) });
  return true;
}
