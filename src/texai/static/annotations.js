// The author's `%@` annotations, drawn in the page margins.
//
// They live in the LaTeX source as comment lines (see annotations.py), so TeX
// never typesets them; the backend places each one beside the paragraph it
// belongs to, in the same pass that places the paragraph guide's bars. An
// `intent` reads as a label in the left margin, beside its paragraph's bar and
// in its colour; every other kind is a note in the right margin, level with the
// line it comments on. Clicking either opens that line in the editor.
//
// Like the bars, they live in an overlay every page render wipes, so `draw()`
// reruns cheaply on any scroll or zoom; the data arrives with the bars.

const STORAGE_KEY = 'texai:show-notes';
const BAR_GAP = 12; // px: must match paraguide.js, so a label clears its bar
const EDGE = 6; // px kept clear of the page's own edge
// A right margin narrower than this gets a dot instead of a note, which says
// "something here" and keeps the text for the tooltip.
const MIN_NOTE_WIDTH = 72;

const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};

export class AnnotationLayer {
  constructor({ viewer, button }) {
    this.viewer = viewer;
    this.button = button;
    this.items = [];
    this.onEdit = null; // open a source line in the editor
    // On unless switched off: annotations are the author's own, written to be read.
    this.enabled = localStorage.getItem(STORAGE_KEY) !== '0';
    this.button?.addEventListener('click', () => this.toggle());
    this._reflect();
  }

  set(items) {
    this.items = items || [];
    this._reflect();
    this.draw();
  }

  toggle() {
    this.enabled = !this.enabled;
    localStorage.setItem(STORAGE_KEY, this.enabled ? '1' : '0');
    this._reflect();
    this.draw();
  }

  /** The toolbar button only exists for a paper that has annotations. */
  _reflect() {
    if (!this.button) return;
    const n = this.items.length;
    this.button.hidden = n === 0;
    this.button.classList.toggle('active', this.enabled);
    this.button.setAttribute('aria-pressed', String(this.enabled));
    this.button.textContent = `Notes (${n})`;
  }

  clear() {
    for (const entry of this.viewer.pages) {
      entry.el.querySelector('.note-layer')?.remove();
    }
  }

  /** One overlay per page, under the change markers so an open card covers it. */
  _layerFor(entry) {
    let layer = entry.el.querySelector('.note-layer');
    if (!layer) {
      layer = el('div', 'note-layer');
      entry.el.insertBefore(layer, entry.el.querySelector('.mark-layer'));
    }
    return layer;
  }

  draw() {
    this.clear();
    if (!this.enabled || !this.items.length) return;

    // Several intents on one paragraph read as one label.
    const intents = new Map();
    const notes = [];
    for (const item of this.items) {
      if (item.kind !== 'intent') {
        notes.push(item);
        continue;
      }
      const key = `${item.file}#${item.index}`;
      const group = intents.get(key);
      if (group) group.parts.push(item);
      else intents.set(key, { ...item, parts: [item] });
    }

    for (const group of intents.values()) this._drawIntent(group);
    const touched = new Set();
    for (const note of notes) {
      const entry = this._drawNote(note);
      if (entry) touched.add(entry);
    }
    for (const entry of touched) this._unstack(entry);
  }

  /** The page entry and the item's anchor in page pixels, if the page is laid out. */
  _place(item) {
    const entry = this.viewer.pageEntry(item.page);
    if (!entry || !entry.viewport) return null;
    const rect = this.viewer.pdfRectToPageRect(entry, {
      x: item.x,
      y: item.y,
      width: Math.max(item.right - item.x, 1),
      height: 1,
    });
    return { entry, rect };
  }

  _open(node, item) {
    node.addEventListener('click', (event) => {
      // The page underneath treats a click as a reference or a jump.
      event.stopPropagation();
      this.onEdit?.(item.file, item.line);
    });
  }

  _drawIntent(group) {
    const placed = this._place(group);
    if (!placed) return;
    const { entry, rect } = placed;
    const width = rect.left - BAR_GAP - EDGE * 2;
    if (width <= 0) return;

    const text = group.parts.map((part) => part.text).join(' · ');
    const node = el('button', `note-intent p${group.palette}`, text);
    node.type = 'button';
    node.title = `Intent: ${text}\n${group.file}:${group.line} — click to edit`;
    node.style.left = `${EDGE}px`;
    node.style.top = `${rect.top}px`;
    node.style.width = `${width}px`;
    this._open(node, group);
    this._layerFor(entry).append(node);
  }

  _drawNote(item) {
    const placed = this._place(item);
    if (!placed) return null;
    const { entry, rect } = placed;
    const left = rect.left + rect.width + EDGE * 2;
    const width = entry.el.clientWidth - left - EDGE;

    const narrow = width < MIN_NOTE_WIDTH;
    const node = el('button', `note-card p${item.palette}${narrow ? ' dot' : ''}`);
    node.type = 'button';
    node.title = `${item.kind}: ${item.text}\n${item.file}:${item.line} — click to edit`;
    node.style.top = `${rect.top}px`;
    if (narrow) {
      node.style.right = `${EDGE / 2}px`;
    } else {
      node.style.left = `${left}px`;
      node.style.width = `${width}px`;
      if (item.kind !== 'comment') node.append(el('span', 'note-kind', item.kind));
      node.append(el('span', 'note-text', item.text));
    }
    this._open(node, item);
    this._layerFor(entry).append(node);
    return entry;
  }

  /** Push overlapping notes down, as the change cards do, so neighbours stay readable. */
  _unstack(entry) {
    const cards = [...entry.el.querySelectorAll('.note-card')].sort(
      (a, b) => a.offsetTop - b.offsetTop
    );
    let floor = -Infinity;
    for (const card of cards) {
      if (card.offsetTop < floor) card.style.top = `${floor}px`;
      floor = card.offsetTop + card.offsetHeight + 4;
    }
  }
}
