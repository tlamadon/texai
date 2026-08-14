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

const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};

export class MarksLayer {
  constructor({ viewer, button }) {
    this.viewer = viewer;
    this.button = button;
    this.enabled = localStorage.getItem(STORAGE_KEY) === '1';
    this.turnId = null;
    this.marks = [];
    this.openPopover = null;

    if (this.button) {
      this.button.addEventListener('click', () => this.toggle());
      this._renderButton();
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
  }

  /** Point at a turn's changes. Called when a turn finishes. */
  async showTurn(turnId) {
    this.turnId = turnId;
    if (this.enabled) await this.refresh();
  }

  async refresh() {
    if (!this.turnId) {
      this.clear();
      return;
    }
    try {
      const data = await getJSON(`/api/turns/${this.turnId}/marks`);
      this.marks = data.marks || [];
    } catch {
      this.marks = [];
    }
    this._renderButton();
    this.draw();
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
      for (const box of mark.boxes || []) {
        const entry = this.viewer.pageEntry(box.page);
        if (!entry || !entry.viewport) continue;

        let layer = entry.el.querySelector('.mark-layer');
        if (!layer) {
          layer = el('div', 'mark-layer');
          entry.el.append(layer);
        }

        const rect = this.viewer.pdfRectToPageRect(entry, box);
        const node = el('div', `mark ${mark.kind}${mark.accepted ? ' accepted' : ''}`);
        node.style.left = `${rect.left}px`;
        node.style.top = `${rect.top}px`;
        node.style.width = `${Math.max(rect.width, 8)}px`;
        node.style.height = `${Math.max(rect.height, 6)}px`;
        node.title = `${mark.file}:${mark.newStart} — click to review`;
        node.addEventListener('click', (event) => {
          event.preventDefault();
          event.stopPropagation();
          this._openPopover(entry, node, mark);
        });
        layer.append(node);
      }
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
      await postJSON(`/api/turns/${this.turnId}/hunks/${mark.id}/${action}`, {});
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
