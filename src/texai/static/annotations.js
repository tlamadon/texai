// The author's `%@` annotations, drawn in the page margins.
//
// They live in the LaTeX source as comment lines (see annotations.py), so TeX
// never typesets them; the backend places each one beside the paragraph it
// belongs to, in the same pass that places the paragraph guide's bars, and says
// where it goes in `place`, which is all this file decides by. An `intent`
// reads as a label in the left margin, beside its paragraph's bar and in its
// colour; a `note` is a slip in the right margin, level with the line it is
// about. Clicking either opens that line in the editor. A `proposal` is a slip
// too, saying how many versions of the line are on offer; clicking it opens them
// in the Peek tab, to pick from.
//
// A `%@src` is provenance rather than a note: a small icon just right of the
// line it backs, its shape saying what backs it (this document, a project file,
// a paper). Clicking it peeks at the source; Alt-click edits the annotation.
// It has its own toggle, since a sourced paper can carry dozens.
//
// Like the bars, they live in an overlay every page render wipes, so `draw()`
// reruns cheaply on any scroll or zoom; the data arrives with the bars.

const STORAGE_KEY = 'texai:show-notes';
const SOURCES_KEY = 'texai:show-sources';
const ICON = 11; // px: a source marker, square
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

// One glyph per kind of source, drawn on a 12-unit grid.
const GLYPHS = {
  // A section mark: somewhere else in this paper.
  label: 'M8 2.5H5.5a2 2 0 0 0 0 4h1a2 2 0 0 1 0 4H4M6 1.5v10',
  // Angle brackets: code or data in the project.
  file: 'M4.5 3 1.5 6l3 3M7.5 3l3 3-3 3',
  // A page with its corner turned: a paper.
  paper: 'M3 1.5h4l2.5 2.5v6.5H3zM7 1.5V4h2.5M4.8 6.5h3M4.8 8.5h3',
};

const svgIcon = (type) => {
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('viewBox', '0 0 12 12');
  svg.setAttribute('aria-hidden', 'true');
  const path = document.createElementNS(ns, 'path');
  path.setAttribute('d', GLYPHS[type] || GLYPHS.file);
  svg.append(path);
  return svg;
};

const SOURCE_NOUN = { label: 'In this paper', file: 'Project file', paper: 'Paper' };

/** The target as it is written after `%@src`. */
export function sourceTarget(source) {
  if (source.type === 'label') return `#${source.target}`;
  if (source.type === 'paper') return `@${source.target}`;
  if (!source.start) return source.target;
  const end = source.end && source.end !== source.start ? `-${source.end}` : '';
  return `${source.target}:${source.start}${end}`;
}

/** What a source marker's tooltip says it points at. */
export function describeSource(source) {
  const parts = [`${SOURCE_NOUN[source.type] || 'Source'}: ${sourceTarget(source)}`];
  if (source.page) parts.push(`p. ${source.page}`);
  const lines = [parts.join(', ')];
  if (source.quote) lines.push(`\u201c${source.quote}\u201d`);
  if (source.note) lines.push(source.note);
  return lines.join('\n');
}

export class AnnotationLayer {
  constructor({ viewer, button, sourcesButton }) {
    this.viewer = viewer;
    this.button = button;
    this.sourcesButton = sourcesButton;
    this.items = [];
    this.sources = [];
    this.onEdit = null; // open a source line in the editor
    this.onPeek = null; // show what a %@src points at
    // On unless switched off: annotations are the author's own, written to be read.
    this.enabled = localStorage.getItem(STORAGE_KEY) !== '0';
    this.showSources = localStorage.getItem(SOURCES_KEY) !== '0';
    this.button?.addEventListener('click', () => this.toggle());
    this.sourcesButton?.addEventListener('click', () => this.toggleSources());
    this._reflect();
  }

  set(items) {
    const all = items || [];
    this.items = all.filter((item) => item.place !== 'marker');
    this.sources = all.filter((item) => item.place === 'marker' && item.source);
    this._reflect();
    this.draw();
  }

  toggle() {
    this.enabled = !this.enabled;
    localStorage.setItem(STORAGE_KEY, this.enabled ? '1' : '0');
    this._reflect();
    this.draw();
  }

  toggleSources() {
    this.showSources = !this.showSources;
    localStorage.setItem(SOURCES_KEY, this.showSources ? '1' : '0');
    this._reflect();
    this.draw();
  }

  /** The toolbar buttons only exist for a paper that has what they show. */
  _reflect() {
    const show = (button, n, on, label) => {
      if (!button) return;
      button.hidden = n === 0;
      button.classList.toggle('active', on);
      button.setAttribute('aria-pressed', String(on));
      button.textContent = `${label} (${n})`;
    };
    show(this.button, this.items.length, this.enabled, 'Notes');
    show(this.sourcesButton, this.sources.length, this.showSources, 'Sources');
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
    if (this.showSources) this._drawSources();
    if (!this.enabled || !this.items.length) return;

    // Several intents on one paragraph read as one label.
    const intents = new Map();
    const notes = [];
    for (const item of this.items) {
      if (item.place !== 'intent') {
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
      const entry = note.place === 'proposal' ? this._drawProposal(note) : this._drawNote(note);
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

  /** A slip in the right margin, or a dot where there is no room for one. */
  _slip(item, className, title, fill) {
    const placed = this._place(item);
    if (!placed) return null;
    const { entry, rect } = placed;
    const left = rect.left + rect.width + EDGE * 2;
    const width = entry.el.clientWidth - left - EDGE;

    const narrow = width < MIN_NOTE_WIDTH;
    const node = el('button', `note-card ${className} p${item.palette}${narrow ? ' dot' : ''}`);
    node.type = 'button';
    node.title = title;
    node.style.top = `${rect.top}px`;
    if (narrow) {
      node.style.right = `${EDGE / 2}px`;
    } else {
      node.style.left = `${left}px`;
      node.style.width = `${width}px`;
      fill(node);
    }
    this._layerFor(entry).append(node);
    return { entry, node };
  }

  _drawNote(item) {
    const who = [item.by, item.re && `\u21b3 ${item.re}`].filter(Boolean).join(' ');
    const done = item.status === 'done';
    const slip = this._slip(
      item,
      `${item.re ? 'reply' : ''}${done ? ' done' : ''}`,
      `${item.kind}${item.id ? ` ${item.id}` : ''}${who ? ` (${who})` : ''}` +
        `${item.status ? ` [${item.status}]` : ''}: ${item.text}\n` +
        `${item.file}:${item.line} — click to edit`,
      (node) => {
        if (item.re) node.append(el('span', 'note-re', '\u21b3'));
        if (item.kind !== 'comment') node.append(el('span', 'note-kind', item.kind));
        if (item.by) node.append(el('span', 'note-by', item.by));
        node.append(el('span', 'note-text', item.text));
      }
    );
    if (!slip) return null;
    this._open(slip.node, item);
    return slip.entry;
  }

  _drawProposal(item) {
    const count = item.options.length;
    const labels = item.options.map((option) => option.label).join(' \u00b7 ');
    const slip = this._slip(
      item,
      'proposal',
      `${count} proposed versions of ${item.file}:${item.start}: ${labels}\n` +
        'Click to compare and pick \u00b7 Alt-click to edit',
      (node) => {
        node.append(el('span', 'note-kind', `${count} versions`));
        node.append(el('span', 'note-text', labels));
      }
    );
    if (!slip) return null;
    slip.node.addEventListener('click', (event) => {
      event.stopPropagation();
      if (event.altKey) this.onEdit?.(item.file, item.line);
      else this.onPeek?.(item);
    });
    return slip.entry;
  }

  /** Source markers, just right of the line each one backs; several on one
   * line sit side by side, outward. */
  _drawSources() {
    const taken = new Map(); // page#y -> markers already there
    for (const item of this.sources) {
      const placed = this._place(item);
      if (!placed) continue;
      const { entry, rect } = placed;
      const key = `${item.page}#${Math.round(item.y)}`;
      const nth = taken.get(key) || 0;
      taken.set(key, nth + 1);

      const { source } = item;
      const node = el('button', `note-source s-${source.type}`);
      node.type = 'button';
      node.append(svgIcon(source.type));
      node.title = `${describeSource(source)}\nClick to peek \u00b7 Alt-click to edit`;
      node.style.left = `${rect.left + rect.width + 2 + nth * (ICON + 2)}px`;
      node.style.top = `${rect.top + 1}px`;
      node.addEventListener('click', (event) => {
        event.stopPropagation();
        if (event.altKey) this.onEdit?.(item.file, item.line);
        else this.onPeek?.(item);
      });
      this._layerFor(entry).append(node);
    }
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
