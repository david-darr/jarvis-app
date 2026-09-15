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
      stream = await navigator.mediaDevices.getUserMedia({
        // Chromium's own processing is good and costs nothing here; raw mic
        // input into whisper is noticeably worse in a normal room.
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
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
