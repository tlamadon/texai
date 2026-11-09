// The author's `%@` annotations, drawn in the page margins.
//
// They live in the LaTeX source as comment lines (see annotations.py), so TeX
// never typesets them; the backend places each one beside the paragraph it
// belongs to, in the same pass that places the paragraph guide's bars, and says
// where it goes in `place`, which is all this file decides by. An `intent`
// reads as a label in the left margin, beside its paragraph's bar and in its
// colour; a `note` is a slip in the right margin, level with the line it is
// about. Clicking an intent opens its line in the editor; clicking a note opens
// its thread — the note, its replies, and what can be done with them — and
// Alt-click goes to the source instead. A reply is folded into the note it
// answers, which says how many it has. A `proposal` is a slip
// too, saying how many versions of the line are on offer; clicking it opens them
// in the Peek tab, to pick from.
//
// A `%@src` is provenance rather than a note. A paragraph's sources share one
// small icon in the right margin, level with the first sentence they back, with
// a count when there are several. Clicking it lists them in a popup — what each
// links to, its quote or note, and the sentence it backs — and clicking one of
// those peeks at it; Alt-click edits its annotation instead. Sources have their
// own toggle, since a well-sourced paper carries dozens.
//
// Like the bars, they live in an overlay every page render wipes, so `draw()`
// reruns cheaply on any scroll or zoom; the data arrives with the bars.

import { threadRoot } from './comments.js';

const STORAGE_KEY = 'texai:show-notes';
const SOURCES_KEY = 'texai:show-sources';
// Room kept for the source icon between the text and the notes beside it.
const SOURCE_GUTTER = 12;
const POPUP_WIDTH = 320; // px
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
  // Two pages, one behind the other: a paragraph's sources.
  sources: 'M4 1.5h5.5v7M2.5 3.5h5.5v7h-5.5zM4 6h2.5M4 8h2.5',
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

/** "Card, David and Kluve, Jochen and Weber, Andrea" -> "Card, Kluve & Weber". */
export function shortAuthors(field) {
  const names = field.split(/\s+and\s+/i).map((name) => {
    const trimmed = name.trim();
    return trimmed.includes(',') ? trimmed.split(',')[0].trim() : trimmed.split(/\s+/).pop();
  });
  if (names.length > 3) return `${names[0]} et al.`;
  if (names.length === 1) return names[0];
  return `${names.slice(0, -1).join(', ')} & ${names[names.length - 1]}`;
}

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
    this.onThread = null; // (item, items, anchor): open a note's thread
    this.popup = null; // {key, node}: the open list of a paragraph's sources
    this._closeOnOutside = (event) => {
      if (this.popup && !this.popup.node.contains(event.target) && !event.target.closest?.('.note-source')) {
        this.closePopup();
      }
    };
    this._closeOnEscape = (event) => {
      if (event.key === 'Escape') this.closePopup();
    };
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
    if (!this.showSources) this.closePopup();
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
    else this.closePopup();
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
    // A reply rides on the note it answers, when that note is here to carry it.
    const ids = new Set(notes.filter((n) => n.id).map((n) => `${n.file}#${n.id}`));
    const replies = new Map();
    for (const note of notes) {
      if (note.re && ids.has(`${note.file}#${note.re}`)) {
        const root = threadRoot(note, notes);
        const key = `${root.file}#${root.id}`;
        replies.set(key, (replies.get(key) || 0) + 1);
      }
    }
    const touched = new Set();
    for (const note of notes) {
      if (note.re && ids.has(`${note.file}#${note.re}`)) continue;
      const entry =
        note.place === 'proposal'
          ? this._drawProposal(note)
          : this._drawNote(note, replies.get(`${note.file}#${note.id}`) || 0);
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
    // Clear of the source icons, when they are shown, so neither covers the other.
    const gutter = this.showSources && this.sources.length ? SOURCE_GUTTER : 0;
    const left = rect.left + rect.width + EDGE * 2 + gutter;
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

  _drawNote(item, replies = 0) {
    const who = [item.by, item.re && `\u21b3 ${item.re}`].filter(Boolean).join(' ');
    const done = item.status === 'done';
    const answered = replies ? ` \u00b7 ${replies} repl${replies > 1 ? 'ies' : 'y'}` : '';
    const slip = this._slip(
      item,
      `${item.re ? 'reply' : ''}${done ? ' done' : ''}`,
      `${item.kind}${item.id ? ` ${item.id}` : ''}${who ? ` (${who})` : ''}` +
        `${item.status ? ` [${item.status}]` : ''}${answered}: ${item.text}\n` +
        `${item.file}:${item.line} — click to open \u00b7 Alt-click to edit`,
      (node) => {
        if (item.re) node.append(el('span', 'note-re', '\u21b3'));
        if (item.kind !== 'comment') node.append(el('span', 'note-kind', item.kind));
        if (item.by) node.append(el('span', 'note-by', item.by));
        node.append(el('span', 'note-text', item.text));
        if (replies) node.append(el('span', 'note-replies', `\u21b3 ${replies}`));
      }
    );
    if (!slip) return null;
    const key = `${item.file}:${item.line}`;
    slip.node.dataset.key = key;
    slip.node.addEventListener('click', (event) => {
      // The page underneath treats a click as a reference or a jump.
      event.stopPropagation();
      if (event.altKey || !this.onThread) {
        this.onEdit?.(item.file, item.line);
        return;
      }
      this.onThread(item, this.items, () => this._cardFor(key));
    });
    return slip.entry;
  }

  /** A note's card as drawn now, and its page: what an open thread hangs off. */
  _cardFor(key) {
    for (const entry of this.viewer.pages) {
      const node = entry.el.querySelector(`.note-card[data-key="${CSS.escape(key)}"]`);
      if (node) return { node, entry };
    }
    return null;
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

  /** A paragraph's sources, by paragraph and page, in source order. */
  _sourceGroups() {
    const groups = new Map();
    for (const item of this.sources) {
      const key = `${item.file}#${item.index}#${item.page}`;
      const group = groups.get(key);
      if (group) group.items.push(item);
      else groups.set(key, { key, items: [item] });
    }
    for (const group of groups.values()) {
      group.items.sort((a, b) => a.line - b.line);
      // Level with the first sentence they back on this page.
      group.anchor = group.items.reduce((top, item) => (item.y < top.y ? item : top));
    }
    return groups;
  }

  /** One icon per paragraph, just right of the text, with a count if several. */
  _drawSources() {
    const groups = this._sourceGroups();
    for (const group of groups.values()) {
      const placed = this._place(group.anchor);
      if (!placed) continue;
      const { entry, rect } = placed;
      const count = group.items.length;
      const node = el('button', 'note-source');
      node.type = 'button';
      node.dataset.key = group.key;
      node.append(svgIcon('sources'));
      if (count > 1) node.append(el('span', 'note-source-count', String(count)));
      node.title = `${count} source${count > 1 ? 's' : ''} for this paragraph \u2014 click to list`;
      node.style.left = `${rect.left + rect.width + 2}px`;
      node.style.top = `${rect.top + 1}px`;
      node.classList.toggle('open', this.popup?.key === group.key);
      node.addEventListener('click', (event) => {
        event.stopPropagation();
        if (this.popup?.key === group.key) this.closePopup();
        else this._openPopup(group, node, entry);
      });
      this._layerFor(entry).append(node);
    }

    // The open list follows its icon through zooms; it goes if its paragraph did.
    if (this.popup) {
      const group = groups.get(this.popup.key);
      const icon = this._iconFor(this.popup.key);
      if (!group) this.closePopup();
      else if (icon) this._positionPopup(icon.node, icon.entry);
    }
  }

  _iconFor(key) {
    for (const entry of this.viewer.pages) {
      const node = entry.el.querySelector(`.note-source[data-key="${CSS.escape(key)}"]`);
      if (node) return { node, entry };
    }
    return null;
  }

  _openPopup(group, icon, entry) {
    this.closePopup();
    const node = el('div', 'source-popup');
    node.setAttribute('role', 'dialog');
    const head = el('div', 'source-popup-head');
    const count = group.items.length;
    head.append(el('span', '', count > 1 ? `${count} sources` : 'Source'));
    const close = el('button', 'ghost icon', '\u00d7');
    close.type = 'button';
    close.title = 'Close (Esc)';
    close.addEventListener('click', () => this.closePopup());
    head.append(close);
    node.append(head);
    for (const item of group.items) node.append(this._sourceEntry(item));
    // Clicks inside stay inside: the page underneath reads them as jumps.
    node.addEventListener('click', (event) => event.stopPropagation());
    node.addEventListener('mousedown', (event) => event.stopPropagation());
    node.addEventListener('dblclick', (event) => event.stopPropagation());

    this.viewer.container.append(node);
    this.popup = { key: group.key, node };
    icon.classList.add('open');
    this._positionPopup(icon, entry);
    document.addEventListener('mousedown', this._closeOnOutside);
    document.addEventListener('keydown', this._closeOnEscape);
  }

  /** Under the icon, its right edge with the icon's, inside the scroll area. */
  _positionPopup(icon, entry) {
    const { node } = this.popup;
    const rect = this.viewer.pageRectToContainer(entry, {
      left: icon.offsetLeft,
      top: icon.offsetTop,
      width: icon.offsetWidth,
      height: icon.offsetHeight,
    });
    const room = this.viewer.container.scrollWidth;
    const width = Math.min(POPUP_WIDTH, room - 16);
    const left = Math.max(8, Math.min(rect.left + rect.width - width, room - width - 8));
    node.style.width = `${width}px`;
    node.style.left = `${left}px`;
    node.style.top = `${rect.top + rect.height + 4}px`;
  }

  closePopup() {
    if (!this.popup) return;
    this.popup.node.remove();
    document.querySelectorAll('.note-source.open').forEach((icon) => icon.classList.remove('open'));
    this.popup = null;
    document.removeEventListener('mousedown', this._closeOnOutside);
    document.removeEventListener('keydown', this._closeOnEscape);
  }

  /** One source in the list: what it links to, what it says, what it backs. */
  _sourceEntry(item) {
    const { source, bib } = item;
    const row = el('button', `source-entry s-${source.type}`);
    row.type = 'button';
    row.title = `${describeSource(source)}\nClick to open \u00b7 Alt-click to edit`;
    row.append(svgIcon(source.type));

    const body = el('div', 'source-entry-body');
    const title = bib?.title || (source.type === 'label' ? source.target : sourceTarget(source));
    body.append(el('div', 'source-entry-title', title));

    const meta = [];
    if (source.type === 'paper') {
      const who = [bib?.author && shortAuthors(bib.author), bib?.year].filter(Boolean).join(', ');
      if (who) meta.push(who);
      meta.push(`@${source.target}`);
    } else if (source.type === 'label') {
      meta.push('in this paper');
    } else {
      meta.push('project file');
    }
    if (source.page) meta.push(`p. ${source.page}`);
    body.append(el('div', 'source-entry-meta', meta.join(' \u00b7 ')));
    if (source.quote) body.append(el('div', 'source-entry-quote', `\u201c${source.quote}\u201d`));
    if (source.note) body.append(el('div', 'source-entry-note', source.note));
    if (item.backs) body.append(el('div', 'source-entry-backs', `\u21b3 ${item.backs}`));
    row.append(body);

    row.addEventListener('click', (event) => {
      event.stopPropagation();
      this.closePopup();
      if (event.altKey) this.onEdit?.(item.file, item.line);
      else this.onPeek?.(item);
    });
    return row;
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
