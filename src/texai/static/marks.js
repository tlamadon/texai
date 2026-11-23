// Inline change markers drawn on the PDF.
//
// The backend forward-maps each changed source line through `synctex view` to
// the boxes it produced in the rebuilt PDF; this draws a band over each one and
// hangs an accept/reject popover off it. Off by default — it is an option, and
// remembered across reloads.

import { getJSON, postJSON } from './api.js';
import { showToast } from './toast.js';

const STORAGE_KEY = 'texai:show-changes';
const AUTO_KEY = 'texai:auto-accept';
// How a change's card shows it: the old words struck through where the new
// ones stand; the old text and the new, one above the other; or the new alone.
const DIFF_KEY = 'texai:diff-view';
const DIFF_VIEWS = [
  { key: 'inline', label: 'inline', title: 'One text: removed words struck through, added words marked' },
  { key: 'split', label: 'before / after', title: 'The old text, then the new' },
  { key: 'new', label: 'new only', title: 'Only the new text, with what was added marked' },
];
const diffView = () => {
  const stored = localStorage.getItem(DIFF_KEY);
  return DIFF_VIEWS.some((v) => v.key === stored) ? stored : 'inline';
};

/** A change as one run of text: what stayed, struck what went, marked what came. */
function inlineNode(parts) {
  const node = el('div', 'mark-inline');
  let used = 0;
  for (const part of parts) {
    if (used >= MAX_CARD_TEXT) {
      node.append(el('span', 'w-same', '…'));
      break;
    }
    const text = part.text.replace(/\s+/g, ' ').slice(0, MAX_CARD_TEXT - used);
    if (!text) continue;
    used += text.length;
    node.append(el('span', `w-${part.op}`, text));
  }
  return node;
}
const MAX_POPOVER_TEXT = 1200;
const MAX_CARD_TEXT = 320;

/**
 * Render a side of the change, emphasising only the words that actually
 * differ. Striking a whole wrapped line when three words changed makes the
 * reader hunt for the difference.
 */
function partsNode(className, parts, fallback) {
  const node = el('div', className);
  if (!parts || !parts.length) {
    node.textContent = trimText(fallback);
    return node;
  }
  let used = 0;
  for (const part of parts) {
    if (used >= MAX_CARD_TEXT) {
      node.append(el('span', 'w-same', '…'));
      break;
    }
    const text = part.text.replace(/\s+/g, ' ').slice(0, MAX_CARD_TEXT - used);
    if (!text) continue;
    used += text.length;
    node.append(el('span', part.changed ? 'w-changed' : 'w-same', text));
  }
  return node;
}

/** Source text for display: one paragraph, capped, without the trailing newline. */
function trimText(text) {
  const flat = (text || '').replace(/\s+/g, ' ').trim();
  return flat.length > MAX_CARD_TEXT ? `${flat.slice(0, MAX_CARD_TEXT - 1)}…` : flat;
}

const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};


const escapeRegExp = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

/**
 * The words that are new in this change, as phrases that stand a chance of
 * appearing literally in the rendered text.
 *
 * Tokens carrying LaTeX markup are dropped: `$\alpha$` renders as a glyph, not
 * as those characters, so searching for it would only ever fail. What is left
 * are runs of plain words, which is what we can actually find on the page.
 */
export function changedPhrases(parts) {
  const phrases = [];
  for (const part of parts || []) {
    if (!part.changed) continue;
    let run = [];
    for (const token of part.text.split(/\s+/)) {
      if (!token) continue;
      if (/[\\${}~^_&%#]/.test(token)) {
        if (run.length) phrases.push(run.join(' '));
        run = [];
      } else {
        run.push(token);
      }
    }
    if (run.length) phrases.push(run.join(' '));
  }
  // Very short fragments match everywhere; they would highlight the wrong words.
  return phrases.filter((p) => p.replace(/[^\p{L}\p{N}]/gu, '').length >= 3);
}

/**
 * What to look for on the page, for a spot a click landed on in the source.
 *
 * Returned longest-first, because one word is often too little to be precise —
 * "the" occurs three times in a rendered line — and too much is easy to miss:
 * a five-word run that straddles a line break, a ligature or a hyphenation is
 * not on the page as written. The caller tries them in order and takes the
 * first that lands. Markup breaks a run for the same reason as above: `$\alpha$`
 * is a glyph, not those characters.
 */
export function phraseAt(text, column, { span = 2 } = {}) {
  const tokens = [];
  for (const match of String(text || '').matchAll(/\S+/g)) {
    tokens.push({ text: match[0], start: match.index, end: match.index + match[0].length });
  }
  if (!tokens.length) return [];

  let index = tokens.findIndex((t) => column >= t.start && column <= t.end);
  if (index === -1) index = tokens.findIndex((t) => t.start > column);
  if (index === -1) index = tokens.length - 1;

  const plain = (token) => !/[\\${}~^_&%#]/.test(token.text) && /[\p{L}\p{N}]/u.test(token.text);
  if (!plain(tokens[index])) return []; // the click is on markup: nothing to find

  const candidates = [];
  for (let reach = span; reach >= 0; reach -= 1) {
    let from = index;
    let to = index;
    while (from > index - reach && from > 0 && plain(tokens[from - 1])) from -= 1;
    while (to < index + reach && to < tokens.length - 1 && plain(tokens[to + 1])) to += 1;
    const phrase = tokens
      .slice(from, to + 1)
      .map((t) => t.text)
      .join(' ');
    if (!candidates.includes(phrase)) candidates.push(phrase);
  }
  return candidates.filter((p) => p.replace(/[^\p{L}\p{N}]/gu, '').length >= 3);
}

/** Text-layer spans whose vertical extent overlaps a band, in reading order. */
function spansInBand(entry, bandRect) {
  const pageRect = entry.el.getBoundingClientRect();
  return [...entry.el.querySelectorAll('.textLayer span')].filter((span) => {
    const r = span.getBoundingClientRect();
    const top = r.top - pageRect.top;
    const bottom = r.bottom - pageRect.top;
    return bottom > bandRect.top + 1 && top < bandRect.top + bandRect.height - 1;
  });
}

/**
 * Rectangles covering just the changed words inside a band.
 *
 * Returns [] when the words cannot be found — hyphenation, ligatures and
 * anything the source spells differently from the render — and the caller then
 * keeps the whole-line band rather than highlighting nothing.
 */
export function narrowRects(entry, bandRect, phrases) {
  const spans = spansInBand(entry, bandRect);
  if (!spans.length || !phrases.length) return [];

  // One string across the band, remembering which text node each offset is in.
  let text = '';
  const segments = [];
  for (const span of spans) {
    const node = span.firstChild;
    if (!node || node.nodeType !== Node.TEXT_NODE) continue;
    segments.push({ node, start: text.length, end: text.length + node.data.length });
    text += node.data;
    if (!/\s$/.test(node.data)) text += ' '; // spans usually abut without a space
  }
  if (!segments.length) return [];

  const at = (offset) => {
    for (const segment of segments) {
      if (offset >= segment.start && offset <= segment.end) {
        return { node: segment.node, offset: offset - segment.start };
      }
    }
    const last = segments[segments.length - 1];
    return { node: last.node, offset: last.end - last.start };
  };

  const pageRect = entry.el.getBoundingClientRect();
  const rects = [];
  for (const phrase of phrases) {
    const pattern = phrase.split(/\s+/).map(escapeRegExp).join('\\s+');
    const match = new RegExp(pattern, 'i').exec(text);
    if (!match) continue;

    const from = at(match.index);
    const to = at(match.index + match[0].length);
    const range = document.createRange();
    try {
      range.setStart(from.node, from.offset);
      range.setEnd(to.node, to.offset);
    } catch {
      continue;
    }
    for (const r of range.getClientRects()) {
      if (r.width < 1 || r.height < 1) continue;
      rects.push({
        left: r.left - pageRect.left,
        top: r.top - pageRect.top,
        width: r.width,
        height: r.height,
      });
    }
  }
  return rects;
}

export class MarksLayer {
  constructor({ viewer, button, acceptAllButton }) {
    this.viewer = viewer;
    this.button = button;
    this.acceptAllButton = acceptAllButton;
    this.enabled = localStorage.getItem(STORAGE_KEY) === '1';
    this.marks = [];
    this.pending = 0;
    // Explicit per-change state. Unset means the default: pending changes show
    // their card, accepted ones stay collapsed until asked for.
    this.state = new Map();
    this.anchors = new Map();
    this.onEdit = null; // open a change's line in the source editor
    this.onRetry = null; // a change was sent back to be done again
    // Take every change as it lands, with nothing to click (see setAuto).
    this.auto = localStorage.getItem(AUTO_KEY) === '1';
    this._accepting = false;
    this.isBusy = () => false; // a turn is running: its work is not finished to accept
    // (file, line, page) -> the rendered lines of that line's block on a
    // beamer slide, in PDF points; [] when they are not known yet; null when
    // the page is not a slide.
    this.slideRects = null;

    if (this.button) {
      this.button.addEventListener('click', () => this.toggle());
      this._renderButton();
    }
    if (this.acceptAllButton) {
      this.acceptAllButton.addEventListener('click', () => this.acceptAll());
    }
  }

  /* ---------------- state ---------------- */

  toggle() {
    this.setEnabled(!this.enabled);
  }

  setEnabled(enabled) {
    this.enabled = enabled;
    localStorage.setItem(STORAGE_KEY, enabled ? '1' : '0');
    this._renderButton();
    if (enabled) this.refresh();
    else this.clear();
  }

  /**
   * Accept changes as they land. The text is in the source either way; this
   * only decides whether each change waits on the page to be looked at first.
   * A turn can still be undone from its card in the chat, or through git.
   */
  setAuto(on) {
    this.auto = !!on;
    localStorage.setItem(AUTO_KEY, this.auto ? '1' : '0');
    if (this.auto) this.refresh();
  }

  /**
   * Show the markers now without changing the remembered choice: a request
   * made from the page should land on the page, whatever the toggle says. The
   * button reads as on, so switching them off again is one click.
   */
  reveal() {
    if (!this.enabled) {
      this.enabled = true;
      this._renderButton();
    }
    this.refresh();
  }

  _renderButton() {
    if (!this.button) return;
    this.button.classList.toggle('active', this.enabled);
    const n = this.marks.filter((m) => !m.accepted).length;
    // What is waiting to be accepted or rejected, and how many.
    this.button.textContent = n ? `Pending (${n})` : 'Pending';
    this.button.setAttribute('aria-pressed', String(this.enabled));
    if (this.acceptAllButton) this.acceptAllButton.hidden = !this.enabled || n === 0;
  }

  async refresh() {
    try {
      // Session-scoped: every change still pending review, not just the last
      // turn's, and always described against the state the review started from.
      const data = await getJSON('/api/changes');
      this.marks = data.marks || [];
      this.pending = data.pending || 0;
    } catch {
      this.marks = [];
      this.pending = 0;
    }
    if (this.auto && this.marks.length && !this._accepting && !this.isBusy()) {
      // Accepting empties the list, and the refresh that follows draws that.
      this._accepting = true;
      try {
        await postJSON('/api/changes/accept-all', {});
        this.marks = [];
        this.pending = 0;
      } catch {
        /* still pending, and shown as such */
      } finally {
        this._accepting = false;
      }
    }
    this._renderButton();
    this.draw();
  }

  async acceptAll() {
    try {
      const { accepted } = await postJSON('/api/changes/accept-all', {});
      showToast(accepted ? `Accepted ${accepted} change${accepted === 1 ? '' : 's'}.` : 'Nothing pending.');
      await this.refresh();
    } catch (err) {
      showToast(err.message || String(err), { type: 'error' });
    }
  }

  /** Whether this change's card should be showing. */
  _isOpen(mark) {
    const explicit = this.state.get(mark.id);
    if (explicit) return explicit === 'open';
    return !mark.accepted;
  }

  clear() {
    for (const entry of this.viewer.pages) {
      entry.el.querySelector('.mark-layer')?.remove();
    }
    this._renderButton();
  }

  /* ---------------- drawing ---------------- */

  /** Re-position every marker. Cheap enough to run on any render or zoom. */
  draw() {
    for (const entry of this.viewer.pages) {
      entry.el.querySelector('.mark-layer')?.remove();
    }
    if (!this.enabled || !this.marks.length) return;

    for (const mark of this.marks) {
      const open = this._isOpen(mark);
      let last = null;

      const phrases = changedPhrases(mark.afterParts);

      // Resolve every box first. A rewrapped paragraph spans several source
      // lines even when only a few words differ, so most of its lines match
      // nothing — and falling back to a line band per box would highlight the
      // lines above and below the words that actually changed. The fallback
      // therefore belongs to the change as a whole: narrow it everywhere we
      // can, and only band whole lines when we could not narrow it anywhere.
      const placements = [];
      for (const box of mark.boxes || []) {
        const entry = this.viewer.pageEntry(box.page);
        if (!entry || !entry.viewport) continue;
        // On a beamer slide the box is the whole slide, which is all SyncTeX
        // knows. The lines of the block that changed are known from the
        // source, so the change is looked for, and failing that banded, there
        // and not across the slide. Until the slide's text is up there is
        // nothing to place it by, and nothing is drawn.
        const lines = this.slideRects?.(mark.file, mark.newStart, box.page);
        const bands = lines
          ? lines.map((line) => this.viewer.pdfRectToPageRect(entry, line))
          : [this.viewer.pdfRectToPageRect(entry, box)];
        for (const rect of bands) {
          placements.push({
            entry,
            layer: this._layerFor(entry),
            rect,
            box,
            narrow: narrowRects(entry, rect, phrases),
          });
        }
      }
      const narrowed = placements.some((p) => p.narrow.length);

      for (const { entry, layer, rect, box, narrow } of placements) {
        if (narrowed && !narrow.length) {
          last = { entry, layer, rect }; // still anchors the card
          continue;
        }
        const targets = narrowed ? narrow : [rect];

        for (const target of targets) {
          const node = el(
            'div',
            `mark ${mark.kind}${mark.accepted ? ' accepted' : ''}${narrowed ? ' narrow' : ''}`
          );
          node.style.left = `${target.left}px`;
          node.style.top = `${target.top}px`;
          node.style.width = `${Math.max(target.width, 8)}px`;
          node.style.height = `${Math.max(target.height, 6)}px`;
          node.title = `${mark.file}:${mark.newStart} — click to see the change`;
          // Remember the *box*, not the rendered rect or its layer: layers are
          // rebuilt on every draw, and pixel rects go stale the moment you zoom.
          const here = { page: box.page, box };
          node.addEventListener('click', (event) => {
            event.preventDefault();
            event.stopPropagation();
            this._open(mark, here); // show it next to the band actually clicked
          });
          layer.append(node);
        }
        last = { entry, layer, rect };
      }

      // The old text is not on the page — LaTeX never typeset it — so the card
      // below the change is where "before" actually gets shown.
      if (open) this._drawCard(this._resolveAnchor(mark) || last, mark);
    }

    for (const entry of this.viewer.pages) this._unstack(entry);
  }

  _layerFor(entry) {
    let layer = entry.el.querySelector('.mark-layer');
    if (!layer) {
      layer = el('div', 'mark-layer');
      entry.el.append(layer);
    }
    return layer;
  }

  /**
   * Clicking a highlight always shows its card — never hides it.
   *
   * Toggling read as "nothing happened": a change's card is anchored to the
   * last line it touches, which can be some way from the band you clicked, so
   * a toggle would quietly close a card you could not see. Collapsing is the
   * card's own x button.
   */
  _open(mark, anchor) {
    this.state.set(mark.id, 'open');
    if (anchor) this.anchors.set(mark.id, anchor);
    this.draw();
  }

  _close(mark) {
    this.state.set(mark.id, 'closed');
    this.anchors.delete(mark.id);
    this.draw();
  }

  /** Turn a remembered click into a live anchor for the current render. */
  _resolveAnchor(mark) {
    const remembered = this.anchors.get(mark.id);
    if (!remembered) return null;
    const entry = this.viewer.pageEntry(remembered.page);
    if (!entry || !entry.viewport) return null;
    return {
      entry,
      layer: this._layerFor(entry),
      rect: this.viewer.pdfRectToPageRect(entry, remembered.box),
    };
  }

  _drawCard(anchor, mark) {
    if (!anchor) return;
    const { entry, layer, rect } = anchor;
    const card = el('div', 'mark-card');
    card.dataset.markId = mark.id;

    const head = el('div', 'mark-card-head');
    // The location doubles as the way into the editor at that exact line.
    const where = el('button', 'mark-where', `${mark.file}:${mark.newStart}`);
    where.title = 'Open this line in the editor';
    where.addEventListener('click', (event) => {
      event.stopPropagation();
      this.onEdit?.(mark.file, mark.newStart);
    });
    head.append(where);
    head.append(el('span', 'spacer'));

    const accept = el('button', 'mark-accept', 'Accept');
    accept.title = 'Keep this change for good. Undoing it later is for git, or the turn\'s Revert';
    accept.addEventListener('click', (event) => {
      event.stopPropagation();
      this._act(mark, 'accept', accept);
    });
    const reject = el('button', 'mark-reject', 'Reject');
    reject.title = 'Put the original text back';
    reject.addEventListener('click', (event) => {
      event.stopPropagation();
      this._act(mark, 'reject', reject);
    });
    const hide = el('button', 'mark-hide', '×');
    hide.title = 'Hide this change';
    hide.addEventListener('click', (event) => {
      event.stopPropagation();
      this._close(mark);
    });
    head.append(accept, reject);
    if (mark.retry) {
      // The request behind the change is known, so it can be asked again.
      const retry = el('button', 'mark-retry', 'Retry');
      retry.title = 'Put the original text back and ask for this again, done differently';
      retry.addEventListener('click', (event) => {
        event.stopPropagation();
        this._act(mark, 'retry', retry);
      });
      const versions = el('button', 'mark-retry', '×3');
      versions.title = 'Put the original text back and ask for three versions to pick from';
      versions.addEventListener('click', (event) => {
        event.stopPropagation();
        this._act(mark, 'retry', versions, { alternatives: 3 });
      });
      head.append(retry, versions);
    }
    head.append(hide);
    card.append(head);

    // How to read it: a switch, when there is something on both sides to compare.
    const body = el('div', 'mark-body');
    const fill = () => {
      const view = mark.before && mark.after && mark.parts?.length ? diffView() : 'split';
      body.replaceChildren();
      if (view === 'inline') {
        body.append(inlineNode(mark.parts));
        return;
      }
      if (view !== 'new') {
        if (mark.before) body.append(partsNode('mark-del', mark.beforeParts, mark.before));
        else body.append(el('div', 'mark-none', 'nothing here before — this text is new'));
      }
      if (mark.after) body.append(partsNode('mark-add', mark.afterParts, mark.after));
      else body.append(el('div', 'mark-none', 'removed — nothing replaced it'));
    };
    if (mark.before && mark.after && mark.parts?.length) {
      const views = el('div', 'mark-views');
      views.setAttribute('role', 'radiogroup');
      const paint = () => {
        for (const button of views.children) {
          button.setAttribute('aria-checked', String(button.dataset.key === diffView()));
        }
      };
      for (const { key, label, title } of DIFF_VIEWS) {
        const button = el('button', '', label);
        button.title = title;
        button.dataset.key = key;
        button.setAttribute('role', 'radio');
        button.addEventListener('click', (event) => {
          event.stopPropagation();
          localStorage.setItem(DIFF_KEY, key);
          // Every open card reads the same way.
          this.draw();
        });
        views.append(button);
      }
      paint();
      card.append(views);
    }
    fill();
    card.append(body);

    card.style.left = `${Math.max(0, rect.left)}px`;
    card.style.top = `${rect.top + rect.height + 4}px`;
    card.style.maxWidth = `${Math.max(240, entry.el.clientWidth - rect.left - 8)}px`;
    card.addEventListener('click', (event) => event.stopPropagation());
    layer.append(card);
  }

  /** Push overlapping cards down so two nearby changes stay readable. */
  _unstack(entry) {
    const cards = [...entry.el.querySelectorAll('.mark-card')].sort(
      (a, b) => a.offsetTop - b.offsetTop
    );
    let floor = -Infinity;
    for (const card of cards) {
      if (card.offsetTop < floor) card.style.top = `${floor}px`;
      floor = card.offsetTop + card.offsetHeight + 6;
    }
  }

  async _act(mark, action, button, body = {}) {
    button.disabled = true;
    try {
      await postJSON(`/api/changes/${mark.id}/${action}`, body);
      if (action === 'accept') {
        // Final: it leaves the review, and its mark goes with it.
        this.state.delete(mark.id);
        this.anchors.delete(mark.id);
        showToast('Change accepted.');
        await this.refresh();
      } else if (action === 'retry') {
        this.state.delete(mark.id);
        this.anchors.delete(mark.id);
        showToast(body.alternatives ? 'Asking for three versions…' : 'Asking again…');
        this.onRetry?.(body);
      } else {
        this.state.delete(mark.id);
        this.anchors.delete(mark.id);
        showToast(`Rejected — rebuilding ${mark.file}…`);
        // The rebuild changes the PDF; the watcher reloads it and the marks are
        // refreshed once the new render exists.
      }
    } catch (err) {
      button.disabled = false;
      showToast(err.message || String(err), { type: 'error' });
    }
  }
}
