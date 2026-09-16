import { api, el, toast } from '../api.js';
import { refreshSpeechStatus, listInputDevices, getInputDevice, setInputDevice, requestMicrophoneAccess, isRecordingSupported } from '../voiceInput.js';
import { isSpeechOutputSupported, whenVoicesReady, resolveVoice, getPreferredVoice, setPreferredVoice } from '../voiceOutput.js';

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

  // -- devices ---------------------------------------------------------------
  async function drawDevices() {
    if (!isRecordingSupported()) return null;
    const section = el('div', { class: 'card speech-devices' });
    const { devices, labelled } = await listInputDevices();

    const select = el('select', { id: 'speech-input-device', 'aria-label': 'Microphone' });
    select.append(el('option', { value: '', text: 'System default' }));
    for (const device of devices) {
      // An unlabelled device still has to be selectable, so it gets a stable
      // stand-in rather than an empty row.
      const label = device.label || `Microphone ${device.deviceId.slice(0, 6)}`;
      select.append(el('option', { value: device.deviceId, text: label }));
    }
    select.value = getInputDevice();
    select.addEventListener('change', () => {
      setInputDevice(select.value);
      toast(select.value ? 'Microphone updated' : 'Using the system default microphone', 'success');
    });

    section.append(el('div', {}, [
      el('strong', { text: 'Microphone' }),
      el('p', { class: 'meta', text: 'Which device dictation and Open Mic record from.' }),
    ]), el('div', { class: 'speech-model-actions' }, [select]));

    if (!labelled && devices.length) {
      // Browsers withhold device names until a page has been granted access,
      // so the list is real but unreadable. Saying why beats showing blanks.
      section.append(el('p', { class: 'meta' }, [
        'Device names appear once microphone access is allowed. ',
        el('button', {
          type: 'button', class: 'btn quiet', text: 'Allow and show names',
          onclick: async () => {
            try { await requestMicrophoneAccess(); draw(); }
            catch { toast('Microphone access was blocked.', 'error'); }
          },
        }),
      ]));
    }

    return section;
  }

  // -- voice -----------------------------------------------------------------
  async function drawVoice() {
    if (!isSpeechOutputSupported()) return null;
    const voices = await whenVoicesReady();
    if (!root.isConnected) return null;
    // Keeps speech-devices for the shared styling and adds its own hook, so a
    // test can target this section rather than whichever matched first.
    const section = el('div', { class: 'card speech-devices speech-voice' });

    const automatic = resolveVoice(voices);
    const select = el('select', { id: 'speech-voice', 'aria-label': 'Voice' });
    select.append(el('option', {
      value: '',
      // Naming what "automatic" actually resolves to, so the default is not a
      // black box someone has to test to understand.
      text: automatic ? `Automatic (${automatic.name})` : 'Automatic',
    }));
    for (const voice of voices) {
      select.append(el('option', { value: voice.name, text: `${voice.name} · ${voice.lang}` }));
    }
    select.value = getPreferredVoice();
    // A saved voice whose language pack was removed leaves the select with no
    // matching option, which silently displays the first one. Reset instead.
    if (select.value !== getPreferredVoice()) setPreferredVoice('');

    const preview = el('button', { type: 'button', class: 'btn quiet', text: 'Preview' });
    preview.addEventListener('click', () => {
      window.speechSynthesis.cancel();
      const utterance = new SpeechSynthesisUtterance('All systems online. What are we working on today?');
      const chosen = voices.find(v => v.name === select.value) || automatic;
      if (chosen) { utterance.voice = chosen; utterance.lang = chosen.lang; }
      utterance.rate = 1.02;
      window.speechSynthesis.speak(utterance);
    });

    select.addEventListener('change', () => {
      setPreferredVoice(select.value);
      toast(select.value ? 'Voice updated' : 'Using the best available voice', 'success');
    });

    section.append(el('div', {}, [
      el('strong', { text: 'Voice' }),
      el('p', { class: 'meta', text: 'Which voice reads replies aloud in Open Mic.' }),
    ]), el('div', { class: 'speech-model-actions' }, [preview, select]));

    if (!voices.some(v => /^en[-_]GB/i.test(v.lang))) {
      // David asked for a British voice. None is installed, and quietly
      // offering a list of American ones would look like the request was
      // ignored. This says what is missing and exactly how to fix it.
      section.append(el('p', { class: 'meta', text:
        'No British English voice is installed on this computer, so only the American voices are listed. '
        + 'Add one in Windows Settings under Time & language, Speech, Manage voices, Add voices, English (United Kingdom). '
        + 'It appears here after a restart and is then chosen automatically.' }));
    }

    // Stated here because this is exactly where someone looks for it, and
    // silence would read as an oversight rather than a real constraint.
    section.append(el('p', { class: 'meta', text:
      'Spoken replies always play through your system default output. The browser speech engine provides no way to choose a device, so this has to be changed in your operating system sound settings.' }));
    return section;
  }

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

    const deviceSection = await drawDevices();
    if (deviceSection && root.isConnected) parts.push(deviceSection);
    const voiceSection = await drawVoice();
    if (voiceSection && root.isConnected) parts.push(voiceSection);

    parts.push(el('p', { class: 'meta', text: 'Larger models are more accurate and slower. Transcription runs on the processor, so a long recording on a modest machine takes a few seconds.' }));
    root.replaceChildren(...parts);
  }

  await draw();
}
