// Asking about the document as a whole, from the toolbar.
//
// The ✦ on the page is about a passage. Some requests have no passage:
// proofread everything, check that the notation is consistent, say what a
// referee would. This is the same ✦ in the toolbar, for those: a box to type
// the request in, and a few common ones that fill it in to be sent as they are
// or adjusted first. The request goes to the agent as a message with nothing
// attached, exactly as the chat composer would send it, and the chat panel
// opens on the turn.

import { micButton, stopDictation } from './speech.js';

// Ones that only read say so: a request about the whole document should not
// rewrite it by surprise.
const ACTIONS = [
  {
    label: 'Proofread',
    text: 'Proofread the whole document: fix typos, grammar and punctuation. Leave the wording and the meaning as they are beyond that.',
  },
  {
    label: 'Check consistency',
    text: 'Check the whole document for inconsistencies: notation, terminology, numbers quoted in the text against the tables and figures, and references or citations that point nowhere. Report what you find with file and line; do not edit.',
  },
  {
    label: 'Tighten the prose',
    text: 'Tighten the prose throughout the document: cut repetition and filler, keep every claim, result and citation. Go section by section.',
  },
  {
    label: 'Read as a referee',
    text: 'Read the whole document as a referee would. List the main weaknesses in the argument and the exposition, most serious first, each with where it is. Do not edit.',
  },
  {
    label: 'Summarise',
    text: 'Summarise the document in one paragraph, then list its main claims and where each is made. Do not edit.',
  },
];

const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};

export class DocumentAsk {
  /**
   * `button` opens the box, placed under it. `send(text)`
   * hands the request over and resolves to whether it was taken.
   */
  constructor({ button, send }) {
    this.button = button;
    this.send = send;

    const menu = el('div', 'editor-menu doc-ask-menu');
    menu.hidden = true;
    const input = el('textarea', 'ask-input');
    input.rows = 3;
    input.placeholder = 'What should be done with the whole document?';
    const actions = el('div', 'doc-ask-actions');
    for (const action of ACTIONS) {
      const item = el('button', 'menu-item', action.label);
      item.type = 'button';
      item.title = action.text;
      item.addEventListener('click', () => {
        input.value = action.text;
        input.focus();
      });
      actions.append(item);
    }
    const bar = el('div', 'ask-bar');
    const hint = el('span', 'ask-hint', 'Enter to send · Shift-Enter for a new line');
    const go = el('button', 'primary', 'Send');
    go.type = 'button';
    go.addEventListener('click', () => this.submit());
    const mic = micButton({ input, onError: (message) => (hint.textContent = message) });
    bar.append(hint, ...(mic ? [mic] : []), go);
    menu.append(input, actions, bar);
    // On the body: the page's pane is a container, and a fixed box inside one
    // is placed against the pane, not the window.
    document.body.append(menu);
    this.menu = menu;
    this.input = input;

    button.addEventListener('click', () => this.toggle(menu.hidden));
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        this.submit();
      }
    });
    document.addEventListener('mousedown', (event) => {
      if (menu.hidden || menu.contains(event.target) || button.contains(event.target)) return;
      this.toggle(false);
    });
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && !menu.hidden) this.toggle(false);
    });
  }

  toggle(open) {
    this.menu.hidden = !open;
    this.button.setAttribute('aria-expanded', String(open));
    this.button.classList.toggle('on', open);
    if (open) {
      // Under the button, and on screen: the button is mid-row on a phone,
      // where a box hung from either of its edges would run off the side.
      const at = this.button.getBoundingClientRect();
      const width = this.menu.offsetWidth;
      const left = Math.max(8, Math.min(at.right - width, window.innerWidth - width - 8));
      Object.assign(this.menu.style, { left: `${left}px`, top: `${at.bottom + 5}px` });
      this.input.focus();
    } else {
      stopDictation();
    }
  }

  async submit() {
    stopDictation();
    const text = this.input.value.trim();
    if (!text) return;
    // Kept if it was not taken (the agent is busy, say), to send again.
    if (await this.send(text)) {
      this.input.value = '';
      this.toggle(false);
    }
  }
}
