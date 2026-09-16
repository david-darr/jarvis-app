// static/js/openMic.js — continuous back-and-forth voice for a chat session
// (David's ask 2026-09-15).
//
// The loop: listen, detect an utterance, transcribe it, send it as a normal
// chat message, speak the reply, listen again. Speaking while the assistant is
// talking cuts it off.
//
// Voice turns are ordinary messages. They stream, persist and appear in the
// transcript exactly like typed ones, so leaving Open Mic leaves a real
// conversation behind rather than a separate voice log that has to be merged.
//
// Endpointing is done in the browser from audio levels rather than with a VAD
// library. voice-line uses webrtcvad server-side, but that would mean shipping
// audio continuously to the backend just to decide whether someone is talking.
// Keeping it local means the microphone stream never leaves the page until an
// utterance is complete.
//
// The honest limitation, the same one voice-line's ears.py documents: an open
// microphone hears the room. Audio from a video, music, or someone else's
// conversation can be picked up as speech. That is why this is a mode you turn
// on deliberately, not the default.

import { createRecorder, transcribeBlob } from './voiceInput.js';
import { createSpeaker, isSpeechOutputSupported } from './voiceOutput.js';

// Tuned against normal speech at a normal distance. The onset threshold sits
// above room tone; the barge-in threshold is higher because the microphone
// also hears the assistant's own voice through the speakers, and a too-low
// value makes it interrupt itself.
const SPEECH_LEVEL = 0.12;
const BARGE_LEVEL = 0.22;
// Speech has gaps. Ending an utterance on the first quiet frame would cut
// people off mid-sentence, so silence has to persist.
const SILENCE_MS = 900;
const MIN_UTTERANCE_MS = 350;
// A hard ceiling so a stuck recorder or a noisy room cannot record forever.
const MAX_UTTERANCE_MS = 45000;

export function createOpenMic({ sessionId, sendMessage, stopTurn, onState, onError }) {
  let state = 'idle';
  let recorder = null;
  let speaker = null;
  let stopped = false;

  let speechStartedAt = 0;
  let lastLoudAt = 0;
  let capturing = false;

  const setState = (next) => {
    if (state === next) return;
    state = next;
    onState?.(next);
  };

  const fail = (message) => {
    onError?.(message);
  };

  // Level callback drives the whole machine. Kept in one place so the states
  // cannot disagree about what the microphone is currently hearing.
  const onLevel = (level) => {
    if (stopped) return;
    const now = Date.now();

    if (state === 'speaking') {
      // Barge-in. Cutting off both the voice and the in-flight turn, because
      // letting the reply finish generating invisibly would make the next
      // thing said arrive against stale context.
      if (level >= BARGE_LEVEL) {
        speaker?.cancel();
        stopTurn?.(sessionId);
        beginCapture(now);
      }
      return;
    }

    if (state !== 'listening') return;

    if (level >= SPEECH_LEVEL) {
      if (!capturing) beginCapture(now);
      lastLoudAt = now;
      return;
    }
    if (!capturing) return;
    if (now - lastLoudAt < SILENCE_MS) return;
    if (now - speechStartedAt < MIN_UTTERANCE_MS) { capturing = false; return; }
    finishUtterance();
  };

  function beginCapture(now) {
    capturing = true;
    speechStartedAt = now;
    lastLoudAt = now;
    setState('listening');
  }

  async function finishUtterance() {
    if (!recorder || !capturing) return;
    capturing = false;
    setState('transcribing');
    let blob = null;
    try {
      blob = await recorder.stop();
    } catch {
      blob = null;
    }
    recorder = null;
    if (stopped) return;

    let text = '';
    if (blob) {
      try {
        text = await transcribeBlob(blob);
      } catch (error) {
        fail(error.message || 'Transcription failed.');
      }
    }
    if (stopped) return;

    if (!text) {
      // Nothing intelligible. Go straight back to listening rather than
      // sending an empty turn or announcing it, since in a continuous loop
      // that would fire constantly on ordinary room noise.
      await listen();
      return;
    }

    setState('thinking');
    try {
      await sendMessage(text, {
        onChunk: (delta) => speaker?.feed(delta),
        onDone: () => speaker?.finish(),
      });
    } catch (error) {
      fail(error.message || 'That turn could not be sent.');
      await listen();
    }
  }

  async function listen() {
    if (stopped) return;
    if (recorder) { try { recorder.cancel(); } catch { /* already gone */ } }
    capturing = false;
    recorder = createRecorder({ onLevel });
    try {
      await recorder.start();
    } catch (error) {
      recorder = null;
      fail(error && error.name === 'NotAllowedError'
        ? 'Microphone access was blocked. Allow it to use Open Mic.'
        : 'No microphone is available.');
      stop();
      return;
    }
    setState('listening');
    // The ceiling is checked here rather than in the level callback so it
    // still applies to a completely silent stuck stream, which produces no
    // level changes worth acting on.
    const started = Date.now();
    const guard = setInterval(() => {
      if (stopped || !capturing) return;
      if (Date.now() - speechStartedAt > MAX_UTTERANCE_MS) {
        clearInterval(guard);
        finishUtterance();
      }
      void started;
    }, 1000);
    guards.add(guard);
  }

  const guards = new Set();

  return {
    get state() { return state; },

    async start() {
      stopped = false;
      speaker = createSpeaker({
        onStart: () => setState('speaking'),
        // Listening resumes only once the whole queue has drained, or the
        // microphone would hear the assistant's own next sentence.
        onEnd: () => { if (!stopped) listen(); },
      });
      if (!isSpeechOutputSupported()) {
        // Still useful without a voice: it becomes hands-free dictation with
        // replies on screen, which is better than refusing outright.
        fail('This browser cannot speak replies aloud. Open Mic will still listen and reply in text.');
      }
      await listen();
    },

    // Called by the turn driver as the reply streams, so the speaker can start
    // talking before the whole answer has arrived.
    feed(delta) { speaker?.feed(delta); },
    finishReply() { speaker?.finish(); },

    stop() { stop(); },
  };

  function stop() {
    stopped = true;
    guards.forEach(clearInterval);
    guards.clear();
    try { recorder?.cancel(); } catch { /* already released */ }
    recorder = null;
    speaker?.cancel();
    speaker = null;
    capturing = false;
    setState('idle');
  }
}

export const _thresholds = { SPEECH_LEVEL, BARGE_LEVEL, SILENCE_MS, MIN_UTTERANCE_MS, MAX_UTTERANCE_MS };
