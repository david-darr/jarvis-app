// static/js/voiceInput.js — microphone capture and transcription for chat
// (David's ask 2026-09-15).
//
// The browser owns decoding and resampling, deliberately. MediaRecorder gives
// webm/opus, which the backend would need a media decoder to read; Chromium
// already has those codecs, so WebAudio decodes and downsamples to the 16 kHz
// mono WAV whisper.cpp wants and uploads that instead. No ffmpeg or av in the
// bundled runtime, and the same path works in the desktop shell and in a
// phone browser.
//
// Nothing here reaches the network except this app's own backend, and the
// audio is transcribed locally on the machine running it (core/speech.py).

import { api } from './api.js';

const TARGET_RATE = 16000;
// Above this a single utterance is almost certainly a stuck recorder rather
// than speech, and it is also where whisper's cost starts to be felt.
const MAX_SECONDS = 120;

let statusCache = null;

// Cached per page load: a status check on every mic press would add a round
// trip to the one interaction that must feel immediate. refreshSpeechStatus()
// clears it after a model download changes the answer.
export async function getSpeechStatus() {
  if (!statusCache) statusCache = api('/api/speech/status').catch(() => null);
  return statusCache;
}
export function refreshSpeechStatus() { statusCache = null; }

// Recording needs a secure context (or localhost) plus a real device. Checked
// up front so the mic button can be hidden rather than offered and then
// failing on click.
export function isRecordingSupported() {
  return !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder);
}

// -- input device selection --------------------------------------------------
// Which microphone to record from. Persisted per device rather than per user:
// it describes the hardware in front of someone, not an account preference,
// so it should not follow them to another machine.
//
// There is deliberately no matching output-device setting. speechSynthesis has
// no sink control at all - verified against this Electron's own Chromium: no
// setSinkId, nothing on the utterance - so replies play to the system default
// and nothing in this app can change that. HTMLMediaElement.setSinkId does
// exist, so output routing would become possible if speech output ever moved
// to a TTS engine producing audio data rather than the Web Speech API.
const INPUT_DEVICE_KEY = 'jarvis:speech-input-device';

export function getInputDevice() {
  try { return localStorage.getItem(INPUT_DEVICE_KEY) || ''; } catch { return ''; }
}

export function setInputDevice(deviceId) {
  try {
    if (deviceId) localStorage.setItem(INPUT_DEVICE_KEY, deviceId);
    else localStorage.removeItem(INPUT_DEVICE_KEY);
  } catch { /* storage is optional; the choice just will not persist */ }
}

// Labels are only populated once the page has been granted microphone access.
// Before that the browser returns entries with empty labels, so the caller is
// told rather than left rendering a list of blanks.
export async function listInputDevices() {
  if (!navigator.mediaDevices?.enumerateDevices) return { devices: [], labelled: false };
  let devices = [];
  try { devices = await navigator.mediaDevices.enumerateDevices(); } catch { return { devices: [], labelled: false }; }
  const inputs = devices.filter(d => d.kind === 'audioinput');
  return { devices: inputs, labelled: inputs.some(d => d.label) };
}

// Prompting for access is what makes labels readable. Kept separate from
// listing so a settings panel can show the list first and only ask when the
// user actually wants to identify the devices.
export async function requestMicrophoneAccess() {
  const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  stream.getTracks().forEach(track => track.stop());
}

function encodeWav(samples, sampleRate) {
  const buffer = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(buffer);
  const writeText = (offset, text) => {
    for (let i = 0; i < text.length; i += 1) view.setUint8(offset + i, text.charCodeAt(i));
  };
  writeText(0, 'RIFF');
  view.setUint32(4, 36 + samples.length * 2, true);
  writeText(8, 'WAVE');
  writeText(12, 'fmt ');
  view.setUint32(16, 16, true);      // PCM chunk size
  view.setUint16(20, 1, true);       // PCM
  view.setUint16(22, 1, true);       // mono
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true); // byte rate
  view.setUint16(32, 2, true);       // block align
  view.setUint16(34, 16, true);      // bits per sample
  writeText(36, 'data');
  view.setUint32(40, samples.length * 2, true);
  let offset = 44;
  for (let i = 0; i < samples.length; i += 1, offset += 2) {
    const clamped = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(offset, clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff, true);
  }
  return new Blob([view], { type: 'audio/wav' });
}

async function toMono16kWav(blob) {
  const bytes = await blob.arrayBuffer();
  const decodeContext = new (window.AudioContext || window.webkitAudioContext)();
  let decoded;
  try {
    decoded = await decodeContext.decodeAudioData(bytes);
  } finally {
    decodeContext.close();
  }
  if (decoded.duration > MAX_SECONDS) throw new Error('That recording is too long.');

  let samples;
  try {
    // Let the browser resample: an OfflineAudioContext at the target rate does
    // a far better job than decimating samples by hand, and handles the
    // channel mixdown at the same time.
    const offline = new OfflineAudioContext(1, Math.ceil(decoded.duration * TARGET_RATE), TARGET_RATE);
    const source = offline.createBufferSource();
    source.buffer = decoded;
    source.connect(offline.destination);
    source.start();
    samples = (await offline.startRendering()).getChannelData(0);
  } catch {
    // Some engines refuse an OfflineAudioContext at an arbitrary rate. Falling
    // back to linear interpolation keeps dictation working rather than failing
    // outright; quality is lower, which whisper tolerates.
    const channel = decoded.getChannelData(0);
    const ratio = decoded.sampleRate / TARGET_RATE;
    const length = Math.floor(channel.length / ratio);
    samples = new Float32Array(length);
    for (let i = 0; i < length; i += 1) samples[i] = channel[Math.floor(i * ratio)];
  }
  return encodeWav(samples, TARGET_RATE);
}

export async function transcribeBlob(blob) {
  const wav = await toMono16kWav(blob);
  const form = new FormData();
  form.append('audio', wav, 'speech.wav');
  // Not api(): that helper sets a JSON content type, and FormData must set its
  // own multipart boundary.
  const res = await fetch('/api/speech/transcribe', { method: 'POST', body: form });
  if (!res.ok) {
    let detail = 'Transcription failed.';
    try { detail = (await res.json()).detail || detail; } catch { /* keep the default */ }
    throw new Error(detail);
  }
  return (await res.json()).text || '';
}

// One recording session. Kept as an object rather than a start/stop pair of
// functions so the caller can always reach stop() and release the microphone,
// including from an error path — a live mic indicator left on after a failure
// is alarming and looks like the app is listening when it isn't.
export function createRecorder({ onLevel } = {}) {
  let stream = null;
  let recorder = null;
  let chunks = [];
  let levelTimer = null;
  let audioContext = null;

  const release = () => {
    clearInterval(levelTimer);
    levelTimer = null;
    if (audioContext) { audioContext.close().catch(() => {}); audioContext = null; }
    if (stream) { stream.getTracks().forEach(track => track.stop()); stream = null; }
    recorder = null;
  };

  return {
    get active() { return !!recorder && recorder.state === 'recording'; },

    async start() {
      // Chromium's own processing is good and costs nothing here; raw mic
      // input into whisper is noticeably worse in a normal room.
      const constraints = { echoCancellation: true, noiseSuppression: true, autoGainControl: true };
      const chosen = getInputDevice();
      try {
        // `exact` so a chosen device that has been unplugged fails loudly
        // here rather than silently recording from something else - hearing
        // the wrong microphone with no indication why is worse than an error.
        stream = await navigator.mediaDevices.getUserMedia({
          audio: chosen ? { ...constraints, deviceId: { exact: chosen } } : constraints,
        });
      } catch (error) {
        if (!chosen || error.name === 'NotAllowedError') throw error;
        // The saved device is gone. Fall back to the default and clear the
        // stale choice, so dictation keeps working and the setting stops
        // pointing at hardware that no longer exists.
        setInputDevice('');
        stream = await navigator.mediaDevices.getUserMedia({ audio: constraints });
      }
      chunks = [];
      recorder = new MediaRecorder(stream);
      recorder.addEventListener('dataavailable', event => {
        if (event.data && event.data.size) chunks.push(event.data);
      });
      recorder.start();

      if (onLevel) {
        // A live level meter, so it is obvious the mic is actually hearing
        // something rather than silently recording a muted device.
        audioContext = new (window.AudioContext || window.webkitAudioContext)();
        const analyser = audioContext.createAnalyser();
        analyser.fftSize = 512;
        audioContext.createMediaStreamSource(stream).connect(analyser);
        const data = new Uint8Array(analyser.frequencyBinCount);
        levelTimer = setInterval(() => {
          analyser.getByteTimeDomainData(data);
          let peak = 0;
          for (const value of data) peak = Math.max(peak, Math.abs(value - 128));
          onLevel(Math.min(1, peak / 90));
        }, 100);
      }
    },

    // Resolves with the recorded audio, or null if nothing was captured.
    async stop() {
      if (!recorder || recorder.state !== 'recording') { release(); return null; }
      const finished = new Promise(resolve => recorder.addEventListener('stop', resolve, { once: true }));
      recorder.stop();
      await finished;
      const type = chunks[0]?.type || 'audio/webm';
      const blob = chunks.length ? new Blob(chunks, { type }) : null;
      release();
      return blob;
    },

    cancel() { try { recorder?.stop(); } catch { /* already stopped */ } release(); },
  };
}
