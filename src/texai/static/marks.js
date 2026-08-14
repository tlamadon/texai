// Inline change markers drawn on the PDF.
//
// The backend forward-maps each changed source line through `synctex view` to
// the boxes it produced in the rebuilt PDF; this draws a band over each one and
// hangs an accept/reject popover off it. Off by default — it is an option, and
// remembered across reloads.

import { getJSON, postJSON } from './api.js';
import { showToast } from './toast.js';

const STORAGE_KEY = 'texai:show-changes';
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
function narrowRects(entry, bandRect, phrases) {
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
    this.collapsed = new Set();
    this.openPopover = null;

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

  _renderButton() {
    if (!this.button) return;
    this.button.classList.toggle('active', this.enabled);
    const n = this.marks.filter((m) => !m.accepted).length;
    this.button.textContent = this.enabled && n ? `Changes (${n})` : 'Show changes';
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

  clear() {
    this._closePopover();
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
      const collapsed = mark.accepted || this.collapsed.has(mark.id);
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
        const rect = this.viewer.pdfRectToPageRect(entry, box);
        placements.push({
          entry,
          layer: this._layerFor(entry),
          rect,
          narrow: narrowRects(entry, rect, phrases),
        });
      }
      const narrowed = placements.some((p) => p.narrow.length);

      for (const { entry, layer, rect, narrow } of placements) {
        if (narrowed && !narrow.length) {
          last = { entry, layer, rect }; // still anchors the card
          continue;
        }
        const targets = narrowed ? narrow : [rect];

        for (const target of targets) {
          const node = el(
            'div',
            `mark ${mark.kind}${collapsed ? ' accepted' : ''}${narrowed ? ' narrow' : ''}`
          );
          node.style.left = `${target.left}px`;
          node.style.top = `${target.top}px`;
          node.style.width = `${Math.max(target.width, 8)}px`;
          node.style.height = `${Math.max(target.height, 6)}px`;
          node.title = `${mark.file}:${mark.newStart} — click to ${collapsed ? 'expand' : 'collapse'}`;
          node.addEventListener('click', (event) => {
            event.preventDefault();
            event.stopPropagation();
            this._toggle(mark);
          });
          layer.append(node);
        }
        last = { entry, layer, rect };
      }

      // The old text is not on the page — LaTeX never typeset it — so the card
      // below the change is where "before" actually gets shown.
      if (last && !collapsed) this._drawCard(last, mark);
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

  _toggle(mark) {
    if (this.collapsed.has(mark.id)) this.collapsed.delete(mark.id);
    else this.collapsed.add(mark.id);
    this.draw();
  }

  _drawCard({ entry, layer, rect }, mark) {
    const card = el('div', 'mark-card');
    card.dataset.markId = mark.id;

    const head = el('div', 'mark-card-head');
    head.append(el('span', 'mark-where', `${mark.file}:${mark.newStart}`));
    head.append(el('span', 'spacer'));

    const accept = el('button', 'mark-accept', 'Accept');
    accept.title = 'Keep this change';
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
    hide.title = 'Collapse';
    hide.addEventListener('click', (event) => {
      event.stopPropagation();
      this._toggle(mark);
    });
    head.append(accept, reject, hide);
    card.append(head);

    if (mark.before) card.append(partsNode('mark-del', mark.beforeParts, mark.before));
    else card.append(el('div', 'mark-none', 'nothing here before — this text is new'));
    if (mark.after) card.append(partsNode('mark-add', mark.afterParts, mark.after));
    else card.append(el('div', 'mark-none', 'removed — nothing replaced it'));

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

  /* ---------------- popover ---------------- */

  _closePopover() {
    this.openPopover?.remove();
    this.openPopover = null;
  }

  _openPopover(entry, anchor, mark) {
    this._closePopover();

    const pop = el('div', 'mark-popover');
    pop.append(el('div', 'mark-where', `${mark.file}:${mark.newStart}`));

    if (mark.before) {
      const before = el('pre', 'mark-before');
      before.textContent = mark.before.slice(0, MAX_POPOVER_TEXT).replace(/\n$/, '');
      pop.append(before);
    }
    if (mark.after) {
      const after = el('pre', 'mark-after');
      after.textContent = mark.after.slice(0, MAX_POPOVER_TEXT).replace(/\n$/, '');
      pop.append(after);
    }
    if (!mark.before) pop.append(el('div', 'mark-note', 'Added by the agent.'));
    if (!mark.after) pop.append(el('div', 'mark-note', 'Removed by the agent.'));

    const actions = el('div', 'mark-actions');
    const accept = el('button', 'primary', 'Accept');
    accept.addEventListener('click', () => this._act(mark, 'accept', accept));
    const reject = el('button', 'danger', 'Reject');
    reject.addEventListener('click', () => this._act(mark, 'reject', reject));
    actions.append(accept, reject);
    pop.append(actions);

    // Anchor under the band, nudged back inside the page if it would overflow.
    const top = anchor.offsetTop + anchor.offsetHeight + 6;
    pop.style.top = `${top}px`;
    pop.style.left = `${Math.max(4, anchor.offsetLeft)}px`;
    entry.el.append(pop);

    const overflow = pop.getBoundingClientRect().right - entry.el.getBoundingClientRect().right;
    if (overflow > 0) pop.style.left = `${Math.max(4, anchor.offsetLeft - overflow - 8)}px`;

    this.openPopover = pop;
    setTimeout(() => {
      document.addEventListener('click', this._dismiss, { once: true });
    }, 0);
  }

  _dismiss = () => this._closePopover();

  async _act(mark, action, button) {
    button.disabled = true;
    try {
      await postJSON(`/api/changes/${mark.id}/${action}`, {});
      this._closePopover();
      if (action === 'accept') {
        showToast('Change accepted.');
        await this.refresh();
      } else {
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
