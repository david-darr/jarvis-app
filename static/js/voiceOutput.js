// static/js/voiceOutput.js — speaking replies aloud (David's ask 2026-09-15).
//
// Uses the browser's own speechSynthesis with OS voices: offline, zero bundle
// cost, and available in the desktop shell and a phone browser alike. Kokoro
// (what The Bridge uses) sounds better, but it is a separate local server with
// its own model, which is exactly the dependency the rest of this feature
// avoided. It could later become an optional download like the whisper models.
//
// Text is spoken sentence by sentence as it streams rather than waiting for
// the whole reply. The chunking rule is carried over from voice-line's
// _SentenceChunker, which exists because of a real problem: the first sentence
// ships alone for the fastest time-to-first-audio, then sentences go in
// two-sentence breaths so the voice does not sound clipped between every one.

// Matches a sentence boundary only when the next thing genuinely starts a new
// sentence, so "v1.10.0" and "e.g. this" are not split mid-thought.
const SENTENCE_END = /(?<=[.!?])\s+(?=[A-Z0-9"'])/;

// Markdown is written to be read, not heard. Speaking the punctuation makes it
// nearly unintelligible, so it is stripped rather than passed through.
function forSpeech(text) {
  return String(text || '')
    .replace(/```[\s\S]*?```/g, ' (code block) ')
    .replace(/`([^`]+)`/g, '$1')
    .replace(/!\[[^\]]*\]\([^)]*\)/g, ' ')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/^\s{0,3}#{1,6}\s+/gm, '')
    .replace(/(\*\*|__|\*|_|~~)/g, '')
    .replace(/^\s*[-*+]\s+/gm, '')
    .replace(/^\s*>\s?/gm, '')
    .replace(/\|/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

// Sentence chunking, shared by the speaker and its tests. Duplicating this for
// testability would let the tested rule and the spoken rule drift apart, which
// defeats the point of testing it.
function createChunker(onFlush) {
  let buffer = '';
  let pending = [];
  let flushedFirst = false;
  const emit = (sentence) => {
    if (!flushedFirst) { flushedFirst = true; onFlush(sentence); return; }
    pending.push(sentence);
    if (pending.length >= 2) { onFlush(pending.join(' ')); pending = []; }
  };
  return {
    get started() { return flushedFirst; },
    feed(text) {
      buffer += text;
      for (;;) {
        const match = SENTENCE_END.exec(buffer);
        if (!match) break;
        const sentence = buffer.slice(0, match.index + 1).trim();
        buffer = buffer.slice(match.index + match[0].length);
        if (sentence) emit(sentence);
      }
    },
    finish() {
      const leftover = buffer.trim();
      buffer = '';
      if (leftover) emit(leftover);
      if (pending.length) { onFlush(pending.join(' ')); pending = []; }
    },
    reset() { buffer = ''; pending = []; flushedFirst = false; },
  };
}

export function isSpeechOutputSupported() {
  return typeof window !== 'undefined' && 'speechSynthesis' in window && 'SpeechSynthesisUtterance' in window;
}

let preferredVoice = null;
export function listVoices() {
  if (!isSpeechOutputSupported()) return [];
  return window.speechSynthesis.getVoices();
}
export function setPreferredVoice(name) { preferredVoice = name || null; }

function pickVoice() {
  const voices = listVoices();
  if (!voices.length) return null;
  if (preferredVoice) {
    const chosen = voices.find(v => v.name === preferredVoice);
    if (chosen) return chosen;
  }
  // A local voice is preferred over a network one: network voices stall
  // without connectivity and would send text off the machine, which the rest
  // of this feature deliberately avoids.
  return voices.find(v => v.localService && /^en/i.test(v.lang)) || voices.find(v => v.localService) || voices[0];
}

// One speaker per conversation. Holds the sentence buffer and the queue so a
// barge-in can silence everything at once.
export function createSpeaker({ onStart, onEnd } = {}) {
  let cancelled = false;
  let speaking = false;
  let queued = 0;

  const enqueue = (text) => {
    const clean = forSpeech(text);
    if (!clean || cancelled || !isSpeechOutputSupported()) return;
    queued += 1;
    const utterance = new SpeechSynthesisUtterance(clean);
    const voice = pickVoice();
    if (voice) { utterance.voice = voice; utterance.lang = voice.lang; }
    utterance.rate = 1.02;
    utterance.addEventListener('start', () => {
      if (!speaking) { speaking = true; onStart?.(); }
    });
    const settle = () => {
      queued = Math.max(0, queued - 1);
      // Finished means the whole queue drained, not that one utterance ended.
      // Reporting per-utterance would make the Open Mic loop start listening
      // between sentences and hear the assistant's own next sentence.
      if (queued === 0 && !window.speechSynthesis.pending && !window.speechSynthesis.speaking) {
        speaking = false;
        onEnd?.();
      }
    };
    utterance.addEventListener('end', settle);
    utterance.addEventListener('error', settle);
    window.speechSynthesis.speak(utterance);
  };

  const chunker = createChunker(enqueue);

  return {
    get speaking() { return speaking; },
    feed(deltaText) { if (!cancelled) chunker.feed(deltaText); },
    finish() {
      if (cancelled) return;
      chunker.finish();
      // Nothing was ever queued, so no end event is coming to release the
      // caller. Report completion rather than leaving the loop waiting.
      if (queued === 0 && !speaking) onEnd?.();
    },
    cancel() {
      cancelled = true;
      chunker.reset();
      queued = 0;
      if (isSpeechOutputSupported()) {
        try { window.speechSynthesis.cancel(); } catch { /* nothing to stop */ }
      }
      if (speaking) { speaking = false; onEnd?.(); }
    },
  };
}

// Exported for tests: chunking and the markdown rules are the parts with real
// behaviour, and driving them through live speech synthesis would be slow and
// machine-dependent.
export const _forSpeech = forSpeech;
export function _chunk(text) {
  const out = [];
  const chunker = createChunker(sentence => out.push(sentence));
  chunker.feed(text);
  chunker.finish();
  return out;
}
