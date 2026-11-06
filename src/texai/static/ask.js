// Asking the agent about a passage without leaving the page.
//
// Select some text and a small button appears at the end of it; click it (or
// press Cmd/Ctrl-K) and a box opens under the passage for a request. Sending
// it is an ordinary agent turn — the passage with the request attached, as a
// chip with an instruction would send it — so the chat keeps the whole record
// even while its panel is put away. The box stays with the passage while the
// agent works, showing what it is doing, and then shows what it said. Its edits
// arrive the way every edit does, as change markers on the rebuilt page, shown
// for this request even when "Show changes" is off.
//
// When the agent edits something, the change card on the page is the answer —
// it shows the edit and carries Accept and Reject — so the box gets out of its
// way. It stays only when there is something to read and nothing else to show
// it: an answer to a question, a failure, or "no changes".
//
// Everything here lives in the scroll container, like the in-place editor:
// pages wipe whatever is drawn on them each time they re-render. The passage is
// remembered in PDF points, so it follows zoom; after a rebuild the text may
// have moved, which is why the highlight goes once the answer is in.

import { postJSON } from './api.js';
import { showToast } from './toast.js';

const IS_MAC = navigator.platform.toUpperCase().includes('MAC');
const MAX_QUOTE = 90; // characters of the passage quoted at the top of the box
const MAX_RECTS = 200;
const BOX_WIDTH = 380; // px
const GAP = 6; // px between the passage and what hangs off it

const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};

/** A tool as a reader would name it: `mcp__texai__show_in_pdf` is "show in pdf". */
const toolName = (verb) => String(verb || '').replace(/^mcp__[^_]+__/, '').replace(/_/g, ' ');

export class PageAsk {
  /**
   * `select({ page, point, word, selectedText })` resolves the passage to a
   * source location (and records it, as a Cmd-click does). `available()` says whether
   * the agent can take a request at all. `onChanges()` brings the markers up;
   * `onOpenChat()` shows the turn in the chat panel.
   */
  constructor({ viewer, container, select, available, onChanges, onOpenChat }) {
    this.viewer = viewer;
    this.container = container;
    this.select = select;
    this.available = available;
    this.onChanges = onChanges;
    this.onOpenChat = onOpenChat;

    // What is on offer: the passage under the button, before anything is asked.
    this.offer = null;
    // The request in flight or answered: { passage, el, state, turnId, ... }.
    this.ask = null;
    // A turn that finished before /api/chat told us its id.
    this._early = null;

    this.button = el('button', 'ask-button', '✦');
    this.button.type = 'button';
    this.button.title = `Ask the agent about this passage (${IS_MAC ? 'Cmd' : 'Ctrl'}-K)`;
    this.button.hidden = true;
    // Keep the selection: a press on the button must not clear what it is for.
    this.button.addEventListener('mousedown', (event) => event.preventDefault());
    this.button.addEventListener('click', () => this.open());
    container.append(this.button);

    document.addEventListener('mouseup', (event) => {
      // After the browser has settled the selection this release made.
      setTimeout(() => this._afterRelease(event), 0);
    });
    document.addEventListener(
      'mousedown',
      (event) => {
        if (event.target === this.button) return;
        this._hideOffer();
        const a = this.ask;
        // An empty box is not worth keeping open; one with a request typed in,
        // or an answer to read, closes only when asked to.
        if (a && a.state === 'compose' && !a.input.value.trim() && !a.el.contains(event.target)) {
          this.close();
        }
      },
      true
    );
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && this.ask?.state === 'done') {
        this.close();
        return;
      }
      const modifier = IS_MAC ? event.metaKey && !event.ctrlKey : event.ctrlKey && !event.metaKey;
      if (!modifier || event.altKey || event.shiftKey || (event.key !== 'k' && event.key !== 'K')) return;
      if (!this.offer) this._offerSelection();
      if (!this.offer) return;
      event.preventDefault();
      this.open();
    });
  }

  /* ---------------- offering ---------------- */

  _afterRelease(event) {
    // A double-click is the in-place editor's, and a modified click belongs to
    // Cmd-click and Alt-click.
    if (event.button !== 0 || event.detail >= 2) return;
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    if (this.ask && this.ask.el.contains(event.target)) return;
    this._offerSelection();
  }

  /** Put the button at the end of the current selection, if it is a passage on the page. */
  _offerSelection() {
    this._hideOffer();
    if (!this.available() || this.ask?.state === 'sending' || this.ask?.state === 'working') return;
    const info = this.viewer.getSelectionInfo();
    if (!info) return;
    const passage = this._capture(info);
    if (!passage) return;
    this.offer = passage;
    this.button.hidden = false;
    this._placeButton();
  }

  /**
   * The selection, frozen: its text, the point SyncTeX will be asked about, and
   * every line box it covers in PDF points, so it can be redrawn at any zoom.
   */
  _capture(info) {
    const selection = window.getSelection();
    if (!selection?.rangeCount) return null;
    const rects = [];
    for (const r of selection.getRangeAt(0).getClientRects()) {
      if (r.width < 1 || r.height < 1 || rects.length >= MAX_RECTS) continue;
      const entry = this.viewer.pageAtClientPoint(r.left + r.width / 2, r.top + r.height / 2);
      if (!entry?.viewport) continue;
      rects.push({ page: entry.num, ...this.viewer.clientRectToPdf(entry, r) });
    }
    if (!rects.length) return null;
    // Captured now, in page terms: by the time the request is sent the view
    // may have scrolled, and screen coordinates would point somewhere else.
    return {
      text: info.text,
      page: info.entry.num,
      point: this.viewer.clientPointToPdf(info.entry, info.clientX, info.clientY),
      word: this.viewer.wordAtClientPoint(info.clientX, info.clientY),
      rects,
    };
  }

  _hideOffer() {
    this.offer = null;
    this.button.hidden = true;
  }

  /** A rectangle of the passage, in the scroll container's coordinates. */
  _spot(rect) {
    const entry = this.viewer.pageEntry(rect.page);
    if (!entry?.viewport) return null;
    return this.viewer.pageRectToContainer(entry, this.viewer.pdfRectToPageRect(entry, rect));
  }

  _placeButton() {
    if (!this.offer) return;
    const last = this._spot(this.offer.rects[this.offer.rects.length - 1]);
    if (!last) return;
    // Just above the end of the passage, clear of the text it would otherwise
    // cover on its own line.
    this.button.style.left = `${last.left + last.width}px`;
    this.button.style.top = `${last.top - 3}px`;
  }

  /* ---------------- asking ---------------- */

  /** Open the request box on the passage on offer. */
  open() {
    const passage = this.offer;
    if (!passage) return;
    this._hideOffer();
    this.close();
    window.getSelection()?.removeAllRanges();

    const box = el('div', 'ask-box');
    const quote = el('div', 'ask-quote', `“${trim(passage.text, MAX_QUOTE)}”`);
    const input = el('textarea', 'ask-input');
    input.rows = 2;
    input.placeholder = 'What should change here? Or ask about it.';
    const status = el('div', 'ask-status');
    const body = el('div', 'ask-reply');
    body.hidden = true;
    const bar = el('div', 'ask-bar');
    const hint = el('span', 'ask-hint', 'Enter to send · Shift-Enter for a new line · Esc to close');
    const primary = el('button', 'primary', 'Send');
    primary.type = 'button';
    const close = el('button', 'ghost ask-close', '×');
    close.type = 'button';
    close.title = 'Close';
    bar.append(hint, primary);
    box.append(close, quote, input, status, body, bar);

    const highlights = passage.rects.map(() => {
      const node = el('div', 'ask-highlight');
      this.container.append(node);
      return node;
    });
    this.container.append(box);

    this.ask = { passage, el: box, input, status, body, bar, hint, primary, highlights, state: 'compose' };

    input.addEventListener('input', () => autosize(input));
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        this.send();
      } else if (event.key === 'Escape') {
        event.preventDefault();
        this.close();
      }
    });
    primary.addEventListener('click', () => {
      const state = this.ask?.state;
      if (state === 'compose') this.send();
      else if (state === 'working') this.stop();
      else if (state === 'done') this.onOpenChat?.();
    });
    close.addEventListener('click', () => this.close());

    this.place();
    input.focus();
  }

  async send() {
    const a = this.ask;
    if (!a || a.state !== 'compose') return;
    const request = a.input.value.trim();
    if (!request) return;

    this._setState('sending');
    this._setStatus('Sending…');
    const { passage } = a;
    try {
      const result = await this.select({
        page: passage.page,
        point: passage.point,
        word: passage.word,
        selectedText: passage.text,
      });
      if (this.ask !== a) return;
      const source = result.selection.source;
      const response = await postJSON('/api/chat', {
        message: '',
        selections: [
          {
            file: source.file,
            line: source.line,
            word: source.word ?? null,
            column: source.column ?? 1,
            page: passage.page,
            selectedText: passage.text,
            instruction: request,
          },
        ],
      });
      if (this.ask !== a) return;
      a.turnId = response.turn.id;
      a.where = `${source.file}:${source.line}`;
    } catch (err) {
      if (this.ask !== a) return;
      this._setState('compose');
      this._setStatus(err.message || String(err), 'err');
      a.input.focus();
      return;
    }

    this._setState('working');
    this._setStatus(`Working on it — ${a.where}`, 'busy');
    if (this._early?.id === a.turnId) this.turnFinished(this._early);
    this._early = null;
  }

  async stop() {
    try {
      await postJSON('/api/interrupt', {});
      this._setStatus('Stopping…', 'busy');
    } catch (err) {
      this._setStatus(err.message || String(err), 'err');
    }
  }

  /* ---------------- following the turn ---------------- */

  /** One step of a running turn: show the latest, so the wait is not blind. */
  agentEntry(turnId, entry) {
    const a = this.ask;
    if (!a || a.turnId !== turnId || a.state !== 'working') return;
    let line = null;
    if (entry.kind === 'text' && entry.text?.trim()) a.lastText = entry.text.trim();
    if (entry.kind === 'tool_use') line = `${toolName(entry.verb)} ${entry.text || ''}`;
    else if (entry.kind === 'build') line = `build — ${entry.text}`;
    if (line) this._setStatus(line.trim(), 'busy');
  }

  turnFinished(turn) {
    const a = this.ask;
    if (!a) return;
    if (a.state === 'sending') {
      this._early = turn; // the answer beat the request's own reply
      return;
    }
    if (a.turnId !== turn.id) return;

    for (const node of a.highlights) node.remove();
    a.highlights = [];

    const changes = turn.reverted ? [] : turn.changes || [];
    if (changes.length) {
      const files = changes.map((c) => c.file).join(', ');
      this.close();
      this.onChanges?.();
      showToast(`Edited ${files} — accept or reject the change on the page.`);
      return;
    }
    if (turn.status === 'failed' || turn.reverted) {
      this._setStatus(turn.error || 'The edit did not build, so it was undone.', 'err');
    } else {
      this._setStatus('No changes.', '');
    }
    // The agent's last message is its answer; earlier ones are narration
    // ("I'll read the passage first").
    const text = a.lastText || (turn.agentText || '').trim();
    a.body.hidden = !text;
    a.body.textContent = text;
    this._setState('done');
    this.place();
  }

  /* ---------------- the box ---------------- */

  _setState(state) {
    const a = this.ask;
    a.state = state;
    a.el.dataset.state = state;
    a.input.hidden = state !== 'compose';
    a.input.disabled = state !== 'compose';
    for (const node of a.highlights) node.classList.toggle('working', state === 'sending' || state === 'working');
    a.hint.textContent = {
      compose: 'Enter to send · Shift-Enter for a new line · Esc to close',
      sending: '',
      working: 'The page rebuilds when it is done',
      done: 'Esc to close',
    }[state];
    a.primary.textContent = { compose: 'Send', sending: 'Send', working: 'Stop', done: 'Open in chat' }[state];
    a.primary.className = state === 'compose' ? 'primary' : 'ghost';
    a.primary.disabled = state === 'sending';
  }

  _setStatus(text, kind = '') {
    const a = this.ask;
    if (!a) return;
    a.status.textContent = text;
    a.status.className = `ask-status ${kind}`;
  }

  /** Re-place everything. Cheap; runs on every render and zoom. */
  place() {
    this._placeButton();
    const a = this.ask;
    if (!a) return;
    a.passage.rects.forEach((rect, i) => {
      const spot = this._spot(rect);
      const node = a.highlights[i];
      if (!node) return;
      node.hidden = !spot;
      if (!spot) return;
      Object.assign(node.style, {
        left: `${spot.left}px`,
        top: `${spot.top}px`,
        width: `${spot.width}px`,
        height: `${spot.height}px`,
      });
    });

    // Under the passage, starting where it starts on the page it ends on, and
    // kept inside that page so a passage at the right edge does not push it off.
    const { rects } = a.passage;
    const lastPage = rects[rects.length - 1].page;
    const onPage = rects.filter((r) => r.page === lastPage).map((r) => this._spot(r)).filter(Boolean);
    const entry = this.viewer.pageEntry(lastPage);
    if (!onPage.length || !entry?.viewport) return;
    const page = this.viewer.pageRectToContainer(entry, { left: 0, top: 0, width: entry.el.clientWidth, height: 0 });
    const left = Math.min(...onPage.map((r) => r.left));
    const bottom = Math.max(...onPage.map((r) => r.top + r.height));
    const width = Math.min(BOX_WIDTH, page.width - 2 * GAP);
    const x = Math.max(page.left + GAP, Math.min(left, page.left + page.width - width - GAP));
    Object.assign(a.el.style, { left: `${x}px`, top: `${bottom + GAP}px`, width: `${width}px` });
  }

  close() {
    const a = this.ask;
    if (!a) return;
    // Closing the box does not stop the agent: the turn is in the chat either
    // way, and its edits still arrive as markers.
    for (const node of a.highlights) node.remove();
    a.el.remove();
    this.ask = null;
  }

  /** A new PDF: the passage on offer is stale, but a request in flight is not. */
  pdfReloaded() {
    this._hideOffer();
    this.place();
  }
}

function trim(text, max) {
  const flat = text.replace(/\s+/g, ' ').trim();
  return flat.length > max ? `${flat.slice(0, max - 1)}…` : flat;
}

function autosize(textarea) {
  textarea.style.height = 'auto';
  textarea.style.height = `${Math.min(textarea.scrollHeight, 200)}px`;
}
