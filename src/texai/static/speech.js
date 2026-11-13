// Dictation: press the microphone, say it, and the words are typed for you.
//
// Talking is often the quickest way to say what is wrong with a paragraph. One
// button, wherever there is something to type into — the composer, the box
// that asks about a passage, a comment, the editor that opens on the page —
// turns the browser's own speech recognition on, and what it hears goes in at
// the caret as it is heard: provisional words first, replaced by the settled
// ones as the recogniser makes up its mind. Press again, or Esc, to stop.
//
// It is the browser's recogniser (the Web Speech API), not something texai
// runs, and that has two consequences worth being plain about. Not every
// browser has one, in which case there is no button at all. And where it comes
// from decides where your voice goes: Chrome and Edge send the audio to their
// vendor's servers to be transcribed, Safari uses Apple's. Nothing is recorded
// or sent until the button is pressed, and nothing after it is released.

// Looked up when asked, not once: a browser may only expose it after a setting
// or a permission changes, and a test can stand one in.
const recogniser = () => window.SpeechRecognition || window.webkitSpeechRecognition || null;

const MIC =
  '<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true"><path fill="none" ' +
  'stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" ' +
  'd="M8 1.5a2 2 0 0 1 2 2V8a2 2 0 0 1-4 0V3.5a2 2 0 0 1 2-2zM3.5 7.5a4.5 4.5 0 0 0 9 0M8 12v2.5M5.5 14.5h5"/></svg>';

/** Whether this browser can take dictation at all. */
export const speechAvailable = () => !!recogniser();

// Only one microphone listens at a time: starting a second stops the first.
let current = null;

/** Stop whatever is listening, e.g. because its box is being sent or closed. */
export function stopDictation() {
  current?.stop();
}

/** What was said, fitted between what is already there: a space on either
    side where words would otherwise run together. */
function joined(before, said, after = '') {
  if (!said) return '';
  const lead = before && !/\s$/.test(before) && !/^[\s.,;:!?)]/.test(said) ? ' ' : '';
  const tail = /^[\p{L}\p{N}\\$]/u.test(after) ? ' ' : '';
  return lead + said + tail;
}

/**
 * Where dictated text goes. `begin()` notes the caret, `write(text)` puts the
 * words heard so far in place of whatever the last call wrote, `end()` leaves
 * the caret after them.
 */
function textareaTarget(input) {
  let before = '';
  let after = '';
  return {
    begin() {
      const at = input.selectionStart ?? input.value.length;
      const to = input.selectionEnd ?? at;
      before = input.value.slice(0, at);
      after = input.value.slice(to);
    },
    write(said) {
      const text = joined(before, said, after);
      input.value = before + text + after;
      const caret = before.length + text.length;
      input.setSelectionRange(caret, caret);
      // Whatever grows the box with its text, or enables Send, listens here.
      input.dispatchEvent(new Event('input', { bubbles: true }));
    },
    end() {
      input.focus();
    },
  };
}

function codeMirrorTarget(cm) {
  let from = null;
  let to = null;
  let lead = '';
  let rest = '';
  return {
    begin() {
      from = cm.getCursor('from');
      to = cm.getCursor('to');
      lead = cm.getRange({ line: from.line, ch: 0 }, from);
      rest = cm.getRange(to, { line: to.line, ch: to.ch + 1 });
    },
    write(said) {
      const text = joined(lead, said, rest);
      cm.replaceRange(text, from, to, '+dictation');
      to = cm.posFromIndex(cm.indexFromPos(from) + text.length);
      cm.setCursor(to);
    },
    end() {
      cm.focus();
    },
  };
}

/**
 * A microphone button for a textarea (`input`) or a CodeMirror (`cm`), or null
 * when the browser cannot take dictation. `onError(message)` hears what went
 * wrong in words a reader can act on.
 */
export function micButton({ input = null, cm = null, onError = null } = {}) {
  if (!recogniser()) return null;
  const target = cm ? codeMirrorTarget(cm) : textareaTarget(input);

  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'mic-button';
  button.innerHTML = MIC;
  const idle = 'Dictate: speak, and it is typed here (uses your browser’s speech recognition)';
  button.title = idle;
  button.setAttribute('aria-label', 'Dictate');
  button.setAttribute('aria-pressed', 'false');
  // A press must not take the caret out of the box it is about to type into.
  button.addEventListener('mousedown', (event) => event.preventDefault());

  let session = null;

  const stop = () => {
    if (!session) return;
    const { recognition } = session;
    session = null;
    if (current?.stop === stop) current = null;
    try {
      recognition.stop();
    } catch {
      // Already stopped: it ends on its own after a silence.
    }
    button.classList.remove('listening');
    button.setAttribute('aria-pressed', 'false');
    button.title = idle;
    target.end();
  };

  const start = () => {
    current?.stop();
    const recognition = new (recogniser())();
    recognition.lang = document.documentElement.lang || navigator.language || 'en-US';
    recognition.continuous = true;
    recognition.interimResults = true;
    session = { recognition };
    current = { stop };

    target.begin();
    recognition.onresult = (event) => {
      if (session?.recognition !== recognition) return;
      // Everything heard since the button was pressed, settled or not: the
      // recogniser revises its last few words as it goes.
      let said = '';
      for (let i = 0; i < event.results.length; i += 1) said += event.results[i][0].transcript;
      target.write(said.replace(/\s+/g, ' ').trim());
    };
    recognition.onerror = (event) => {
      if (event.error === 'no-speech' || event.error === 'aborted') return;
      const why =
        event.error === 'not-allowed' || event.error === 'service-not-allowed'
          ? 'The browser would not give texai the microphone. Allow it for this page and try again.'
          : event.error === 'audio-capture'
            ? 'No microphone was found.'
            : event.error === 'network'
              ? 'Speech recognition could not reach its service.'
              : `Dictation stopped: ${event.error}.`;
      onError?.(why);
    };
    // It ends itself after a long silence, or when the service drops it.
    recognition.onend = () => {
      if (session?.recognition === recognition) stop();
    };

    try {
      recognition.start();
    } catch (err) {
      session = null;
      current = null;
      onError?.(err.message || String(err));
      return;
    }
    button.classList.add('listening');
    button.setAttribute('aria-pressed', 'true');
    button.title = 'Listening… press again, or Esc, to stop';
  };

  button.addEventListener('click', () => (session ? stop() : start()));
  document.addEventListener(
    'keydown',
    (event) => {
      if (event.key !== 'Escape' || !session) return;
      // This Esc stops the microphone; the box it is typing into stays open.
      event.preventDefault();
      event.stopPropagation();
      stop();
    },
    true
  );
  return button;
}
