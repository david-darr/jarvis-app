import { api, el, toast } from '../api.js';
import { refreshSpeechStatus } from '../voiceInput.js';

// Settings > Workspace > Speech (David's ask 2026-09-15).
//
// Dictation ships with the engine but no model, so without this panel there
// is no way to obtain one and the mic button can only ever refuse. Models are
// several hundred megabytes and land in the shared data directory, so
// downloading is admin-gated the same way adding a model endpoint is; the
// status itself is readable by anyone, since a non-admin still needs to know
// why dictation is unavailable.

const POLL_MS = 700;

export async function renderSpeechPanel(content, status) {
  const isAdmin = !!(status && status.is_admin);
  const root = el('section', { class: 'speech-panel' });
  content.append(root);

  const timers = new Set();
  // Registered so leaving the panel mid-download stops the polling rather
  // than leaving intervals running against a detached DOM.
  const stopPolling = () => { timers.forEach(clearInterval); timers.clear(); };
  const observer = new MutationObserver(() => { if (!root.isConnected) { stopPolling(); observer.disconnect(); } });
  observer.observe(content, { childList: true });

  async function draw() {
    let state;
    try {
      state = await api('/api/speech/status');
    } catch {
      root.replaceChildren(el('p', { class: 'meta', text: 'Speech status could not be loaded.' }));
      return;
    }
    if (!root.isConnected) return;

    const header = el('div', { class: 'view-header' }, [
      el('h2', { text: 'Speech' }),
      el('p', { class: 'meta', text: 'Dictate messages by voice. Audio is transcribed on this machine and never uploaded.' }),
    ]);

    const parts = [header];

    if (!state.engine_available) {
      // A missing binding is a broken build, not something a download fixes,
      // so this deliberately does not offer one.
      parts.push(el('div', { class: 'card' }, [
        el('strong', { text: 'Speech recognition is unavailable in this build.' }),
        el('p', { class: 'meta', text: 'The speech engine could not be loaded, so dictation cannot run. Reinstalling the latest release usually resolves it.' }),
      ]));
      root.replaceChildren(...parts);
      return;
    }

    parts.push(el('p', { class: 'meta', text: state.active_model
      ? `Using the ${state.active_model} model.`
      : 'No model downloaded yet. Dictation stays unavailable until one is.' }));

    for (const model of state.models) {
      const row = el('div', { class: 'card card-row speech-model' });
      const label = el('div', {}, [
        el('strong', { text: `${model.label} · ${model.size_mb} MB` }),
        el('p', { class: 'meta', text: model.description || '' }),
      ]);
      row.append(label);

      if (model.downloaded) {
        row.append(el('span', { class: 'meta', text: model.name === state.active_model ? 'In use' : 'Downloaded' }));
      } else if (!isAdmin) {
        row.append(el('span', { class: 'meta', text: 'An admin can download this' }));
      } else {
        const progress = el('span', { class: 'meta', role: 'status' });
        const button = el('button', { type: 'button', class: 'btn', text: 'Download' });
        button.addEventListener('click', async () => {
          button.disabled = true;
          progress.textContent = 'Starting…';
          try {
            await api(`/api/speech/models/${model.name}/download`, { method: 'POST' });
          } catch (error) {
            progress.textContent = '';
            button.disabled = false;
            toast(error.message || 'Download could not be started.', 'error');
            return;
          }
          const timer = setInterval(async () => {
            if (!root.isConnected) { clearInterval(timer); timers.delete(timer); return; }
            let info;
            try { info = await api(`/api/speech/models/${model.name}/progress`); } catch { return; }
            if (info.total) {
              progress.textContent = `${Math.round((info.completed / info.total) * 100)}%`;
            }
            if (info.done) {
              clearInterval(timer);
              timers.delete(timer);
              if (info.error) {
                progress.textContent = '';
                button.disabled = false;
                toast(`Download failed: ${info.error}`, 'error');
                return;
              }
              // The cached status drives whether the mic button works at all,
              // so it has to be invalidated here or dictation keeps refusing
              // until the page is reloaded.
              refreshSpeechStatus();
              toast(`${model.label} speech model ready`, 'success');
              draw();
            }
          }, POLL_MS);
          timers.add(timer);
        });
        row.append(el('div', { class: 'speech-model-actions' }, [progress, button]));
      }
      parts.push(row);
    }

    parts.push(el('p', { class: 'meta', text: 'Larger models are more accurate and slower. Transcription runs on the processor, so a long recording on a modest machine takes a few seconds.' }));
    root.replaceChildren(...parts);
  }

  await draw();
}
