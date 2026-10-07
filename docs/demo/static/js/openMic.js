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
// Barge-in must be SUSTAINED, not a single loud frame: the microphone hears
// the assistant through the speakers, and one syllable's peak should never
// count as an interruption or it talks over itself.
//
// Counted across a sliding window rather than as a consecutive run. Requiring
// N in a row sounds equivalent and is not: each reading samples a slice of
// audio, and speech dips between syllables, so a real interruption produces a
// broken pattern of loud readings rather than an unbroken one. David talked
// over it for seconds and the run almost never reached three, which is why
// interrupting appeared to do nothing at all.
const BARGE_LOUD_FRAMES = 3;
const BARGE_WINDOW_FRAMES = 6;
// Speech has gaps. Ending an utterance on the first quiet frame would cut
// people off mid-sentence, so silence has to persist.
const SILENCE_MS = 900;
const MIN_UTTERANCE_MS = 350;
// A hard ceiling so a stuck recorder or a noisy room cannot record forever.
const MAX_UTTERANCE_MS = 45000;

// `createMic` and `makeSpeaker` are injection points for the suites, not
// configuration. Barge-in shipped broken because nothing could drive this
// machine without a real microphone and a real voice, so the one test that
// would have caught it could not be written. Defaulted to the real
// implementations; production never passes them.
export function createOpenMic({ sessionId, sendMessage, stopTurn, onState, onError,
                                createMic = createRecorder, makeSpeaker = createSpeaker }) {
  let state = 'idle';
  let recorder = null;
  let speaker = null;
  let stopped = false;

  let speechStartedAt = 0;
  let lastLoudAt = 0;
  let capturing = false;
  let bargeWindow = [];

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

    // Barge-in. The microphone is open the whole time - through transcribing,
    // thinking and speaking - which is the only reason levels arrive here at
    // all. The first version closed the device in finishUtterance() and
    // reopened it from the speaker's onEnd, so nothing was listening while the
    // assistant talked and this branch could never run: barge-in was dead code
    // behind thresholds nothing evaluated. Found by David testing it, not by
    // the suites, which have no microphone to hear.
    if (state === 'thinking' || state === 'speaking') {
      bargeWindow.push(level >= BARGE_LEVEL);
      if (bargeWindow.length > BARGE_WINDOW_FRAMES) bargeWindow.shift();
      if (bargeWindow.filter(Boolean).length < BARGE_LOUD_FRAMES) return;
      bargeWindow = [];
      // Cut both the voice and the in-flight turn: letting the reply finish
      // generating invisibly would make the next thing said land against
      // stale context.
      speaker?.cancel();
      stopTurn?.(sessionId);
      beginCapture(now);
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
    if (now - speechStartedAt < MIN_UTTERANCE_MS) {
      // Too short to be speech - a door, a cough, a keyboard. Drop it rather
      // than only clearing the flag: the recorder is running by now, and
      // leaving it to run would glue the noise onto the front of whatever is
      // actually said next.
      capturing = false;
      discardCapture();
      return;
    }
    finishUtterance();
  };

  function beginCapture(now) {
    capturing = true;
    speechStartedAt = now;
    lastLoudAt = now;
    // Recording starts HERE, not when the device opened. The stream is live
    // continuously for level metering, but capturing continuously would put
    // the assistant's own spoken reply - which the microphone hears through
    // the speakers - at the front of the next utterance sent to whisper.
    recorder?.capture();
    setState('listening');
  }

  // Ends the recording and throws the audio away. Failures are ignored on
  // purpose: this is cleanup, and there is nothing useful to tell anyone about
  // a noise that was never going to be transcribed.
  function discardCapture() {
    bargeWindow = [];
    recorder?.stop().catch(() => {});
  }

  async function finishUtterance() {
    if (!recorder || !capturing) return;
    capturing = false;
    bargeWindow = [];
    setState('transcribing');
    let blob = null;
    try {
      // Ends the utterance but leaves the stream open, so levels keep arriving
      // and the next barge-in is heard.
      blob = await recorder.stop();
    } catch {
      blob = null;
    }
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
      // Nothing intelligible. Back to listening rather than sending an empty
      // turn or announcing it, which in a continuous loop would fire
      // constantly on ordinary room noise.
      setState('listening');
      return;
    }

    setState('thinking');
    // Clears the cancel latch from any previous interruption. Without it the
    // speaker stays permanently silenced after the first barge-in.
    speaker?.reset();
    try {
      await sendMessage(text, {
        onChunk: (delta) => speaker?.feed(delta),
        onDone: () => speaker?.finish(),
      });
    } catch (error) {
      fail(error.message || 'That turn could not be sent.');
      setState('listening');
    }
  }

  // Opens the device without claiming any particular state, and only once per
  // session: the stream stays live from start() to stop(). Reopening it per
  // turn would reacquire the device on every exchange, which on Windows costs
  // a noticeable delay and flickers the recording indicator.
  async function openMicrophone() {
    if (stopped) return false;
    if (recorder) return true;
    capturing = false;
    const active = createMic({ onLevel, gated: true });
    try {
      await active.start();
    } catch (error) {
      fail(error && error.name === 'NotAllowedError'
        ? 'Microphone access was blocked. Allow it to use Open Mic.'
        : 'No microphone is available.');
      stop();
      return false;
    }
    if (stopped) { try { active.cancel(); } catch { /* already gone */ } return false; }
    recorder = active;
    return true;
  }

  async function listen() {
    if (!(await openMicrophone())) return;
    setState('listening');
  }

  // One timer for the session, not one per turn. The original registered a
  // fresh interval on every listen() and cleared none of them, so a long
  // conversation accumulated a live interval per exchange.
  let ceilingTimer = null;

  return {
    get state() { return state; },

    // Exposed so a test can feed levels directly. The real path goes through
    // the recorder's analyser, which a headless suite has no way to drive.
    _level(value) { onLevel(value); },

    async start() {
      stopped = false;
      clearInterval(ceilingTimer);
      // Checked on a timer rather than in the level callback so it still
      // applies to a silent stuck stream, which produces no levels to act on.
      ceilingTimer = setInterval(() => {
        if (stopped || !capturing) return;
        if (Date.now() - speechStartedAt > MAX_UTTERANCE_MS) finishUtterance();
      }, 1000);
      speaker = makeSpeaker({
        onStart: () => setState('speaking'),
        // The device is already open; this only returns the state. It fires
        // once the whole queue has drained rather than per sentence, so the
        // pause between the assistant's own sentences is never mistaken for
        // the end of its turn.
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
    clearInterval(ceilingTimer);
    ceilingTimer = null;
    bargeWindow = [];
    try { recorder?.close(); } catch { /* already released */ }
    recorder = null;
    speaker?.cancel();
    speaker = null;
    capturing = false;
    setState('idle');
  }
}

export const _thresholds = { SPEECH_LEVEL, BARGE_LEVEL, SILENCE_MS, MIN_UTTERANCE_MS, MAX_UTTERANCE_MS, BARGE_LOUD_FRAMES, BARGE_WINDOW_FRAMES };
