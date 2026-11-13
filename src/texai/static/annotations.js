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
// A `%@src` is provenance rather than a note. The sentence it backs gets a faint
// dotted underline, from where the build says it starts to where it ends, and a
// small mark in the right margin, level with the line it ends on; sentences
// ending on one line put their marks side by side. Pointing at a mark lights up
// its sentence. Clicking it opens the source in the Peek tab, or, for a sentence
// with several, lists them in a popup — what each links to, its quote or note —
// to pick from; Alt-click edits the annotation instead. Sources have their own
// toggle, since a well-sourced paper carries dozens.
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


/* ---------------- finding a sentence in the page's text ---------------- */

// What survives any PDF's text extraction: letters and digits, lower-cased,
// accents and ligatures unfolded. The same folding as peek.js and papers.py.
const foldChar = (ch) =>
  ch
    .normalize('NFKD')
    .replace(/\p{M}/gu, '')
    .toLowerCase()
    .replace(/[^\p{L}\p{N}]/gu, '');

export const GAP = '\u0000'; // where the source has math or a reference: rendered, but not as these words

/**
 * A sentence's LaTeX as the words it renders as, folded, with a GAP wherever
 * it renders something its source does not spell out: math, a citation, a
 * reference. Other commands vanish and leave their arguments.
 */
export function texTokens(tex) {
  let text = tex.replace(/(^|[^\\])%.*$/, '$1');
  text = text.replace(/\$\$[\s\S]*?\$\$|\$[^$]*\$|\\\([\s\S]*?\\\)/g, ` ${GAP} `);
  text = text.replace(/\\label\*?\{[^}]*\}/g, ' ');
  text = text.replace(
    /\\(?:[a-zA-Z]*cite[a-zA-Z]*|ref|eqref|autoref|cref|Cref|pageref|footnote|footnotemark)\*?(?:\[[^\]]*\])*\{[^}]*\}/g,
    ` ${GAP} `
  );
  // A command on an argument leaves the argument, joined to its word (an
  // accent: Erd\H{o}s); one on its own is a break between words, as is a
  // control symbol, unless it is an accent (G\'erard).
  text = text.replace(/\\[a-zA-Z]+\*?(?=\{)/g, '').replace(/\\[a-zA-Z]+\*?/g, ' ');
  text = text.replace(/\\['`^"~=.]/g, '').replace(/\\./g, ' ');
  text = text.replace(/[{}]/g, '').replace(/~/g, ' ');
  const tokens = [];
  for (const word of text.split(/\s+/)) {
    if (!word) continue;
    if (word === GAP) {
      if (tokens[tokens.length - 1] !== GAP) tokens.push(GAP);
      continue;
    }
    const folded = [...word].map(foldChar).join('');
    if (folded) tokens.push(folded);
  }
  return tokens;
}

const textIndexes = new WeakMap(); // text layer -> its index

/** A page's drawn text, folded, with where each folded character came from. */
function textIndex(entry) {
  const layer = entry.el.querySelector('.textLayer');
  if (!layer || !layer.querySelector('span')) return null;
  const cached = textIndexes.get(layer);
  if (cached) return cached;
  const raw = []; // [text node, offset, character]
  let folded = '';
  const toRaw = []; // folded index -> raw index
  const walker = document.createTreeWalker(layer, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const text = node.textContent;
    for (let i = 0; i < text.length; i += 1) {
      raw.push([node, i, text[i]]);
      for (const c of foldChar(text[i])) {
        folded += c;
        toRaw.push(raw.length - 1);
      }
    }
    raw.push([null, 0, ' ']); // between runs of text, as a space
  }
  const index = { raw, folded, toRaw };
  textIndexes.set(layer, index);
  return index;
}

/**
 * Whether a sentence stops at raw character `i`: a full stop, question or
 * exclamation mark, then a space or the end of the text, then a capital, a
 * digit (a section number) or nothing. So "et al., 2018)." stops at its last full stop and not at "al.",
 * and "Card et al. (2018)" does not stop at all.
 */
export function stopsAt(raw, i) {
  if (!/[.?!]/.test(raw[i]?.[2] || '')) return false;
  const next = raw[i + 1];
  if (next && next[0] && !/\s/.test(next[2])) return false;
  for (let j = i + 1; j < raw.length && j <= i + 4; j += 1) {
    const ch = raw[j][2];
    if (/\s/.test(ch) || !raw[j][0]) continue;
    return /[\p{Lu}\p{N}]/u.test(ch);
  }
  return true;
}

// How far apart, in letters, two runs of a sentence's words may be when its
// source has something between them that renders as other text.
const MAX_GAP = 80;
// How far past its last word a sentence ending in math or a citation may run
// to reach its full stop.
const MAX_TAIL = 160;

/**
 * Find the sentence's words, run by run, in order, in the page's text; of the
 * places it is found, take the one nearest where the build put it. Returns
 * its lines as rectangles in PDF points.
 */
function matchSentence(index, tokens, entry, viewer, hints) {
  const runs = [];
  let current = '';
  for (const token of tokens) {
    if (token === GAP) {
      if (current) runs.push(current);
      current = '';
      runs.push(GAP);
    } else {
      current += token;
    }
  }
  if (current) runs.push(current);
  const words = runs.filter((run) => run !== GAP);
  if (!words.length || words.join('').length < 3) return null;

  const { folded, toRaw, raw } = index;
  const matches = [];
  for (let at = folded.indexOf(words[0]); at !== -1; at = folded.indexOf(words[0], at + 1)) {
    let position = at + words[0].length;
    let ok = true;
    let gap = false;
    for (const run of runs.slice(runs.indexOf(words[0]) + 1)) {
      if (run === GAP) {
        gap = true;
        continue;
      }
      const next = folded.indexOf(run, position);
      if (next === -1 || next - position > (gap ? MAX_GAP : 0)) {
        ok = false;
        break;
      }
      position = next + run.length;
      gap = false;
    }
    if (ok) matches.push([at, position - 1]);
    if (matches.length > 20) break;
  }
  if (!matches.length) return null;

  // Nearest the build's guess: its first line, by height, then along.
  const hint = hints[0];
  const distance = ([start]) => {
    const [node, offset] = raw[toRaw[start]];
    const range = document.createRange();
    range.setStart(node, offset);
    range.setEnd(node, offset + 1);
    const box = range.getBoundingClientRect();
    const point = viewer.clientPointToPdf(entry, box.left, box.top + box.height / 2);
    return Math.abs(point.y - (hint.base - 3)) * 4 + Math.abs(point.x - hint.x0);
  };
  matches.sort((a, b) => distance(a) - distance(b));
  let [start, end] = matches[0];
  start = toRaw[start];
  end = toRaw[end];

  // Its edges, as the page has them: back over math or a citation it opens
  // with, to where the sentence before it stopped; forward over the
  // punctuation its last word carries, or the math or citation it ends with,
  // to its full stop.
  const leadsWithGap = tokens[0] === GAP;
  const endsWithGap = tokens[tokens.length - 1] === GAP;
  if (leadsWithGap) {
    let i = start - 1;
    while (i > 0 && start - i < MAX_TAIL && !stopsAt(raw, i)) i -= 1;
    if (stopsAt(raw, i)) {
      i += 1;
      while (i < start && (!raw[i][0] || /\s/.test(raw[i][2]))) i += 1;
      start = i;
    }
  }
  if (endsWithGap) {
    let i = end + 1;
    while (i < raw.length - 1 && i - end < MAX_TAIL && !stopsAt(raw, i)) i += 1;
    if (stopsAt(raw, i)) end = i;
  } else {
    while (end + 1 < raw.length && raw[end + 1][0] && /\S/.test(raw[end + 1][2])) end += 1;
  }

  return lineRects(viewer, entry, raw, start, end);
}

/** The boxes of raw characters `start`-`end`, gathered into one rectangle per
    rendered line, in PDF points. Null when none of them is drawn. */
function lineRects(viewer, entry, raw, start, end) {
  const lines = [];
  for (let i = start; i <= end; i += 1) {
    const [node, offset, ch] = raw[i];
    if (!node || /\s/.test(ch)) continue;
    const range = document.createRange();
    range.setStart(node, offset);
    range.setEnd(node, offset + 1);
    const box = range.getBoundingClientRect();
    if (!box.width || !box.height) continue;
    const middle = box.top + box.height / 2;
    const line = lines.find((l) => Math.abs(l.middle - middle) < box.height / 2);
    if (line) {
      line.left = Math.min(line.left, box.left);
      line.right = Math.max(line.right, box.right);
      line.top = Math.min(line.top, box.top);
      line.bottom = Math.max(line.bottom, box.bottom);
    } else {
      lines.push({ middle, left: box.left, right: box.right, top: box.top, bottom: box.bottom });
    }
  }
  if (!lines.length) return null;
  lines.sort((a, b) => a.top - b.top);
  return lines.map((line) => viewer.clientRectToPdf(entry, line));
}

// A sentence longer than this is not one: the page's text ran on through a
// heading, a table or a float with no full stop in it.
const MAX_SENTENCE = 1500;

/**
 * The sentence drawn under a point on a page, as the page has it: its text
 * and one rectangle per rendered line, in PDF points. It runs from where the
 * sentence before it stopped to its own full stop (see `stopsAt`), and never
 * outside `within`, the paragraph's rectangle on the page, when that is given:
 * a heading has no full stop to keep the first sentence under it out.
 * Null when the point is on no text, or what is there is not a sentence.
 */
export function sentenceAt(viewer, entry, clientX, clientY, within = null) {
  const index = textIndex(entry);
  const caret = viewer.caretAt(clientX, clientY);
  if (!index || !caret || caret.node.nodeType !== Node.TEXT_NODE) return null;
  const { raw } = index;
  const length = caret.node.textContent.length;
  const offset = Math.min(caret.offset, Math.max(length - 1, 0));
  const at = raw.findIndex(([node, i]) => node === caret.node && i === offset);
  if (at === -1) return null;

  // Whether a character is inside the paragraph, by where it is drawn.
  const inside = (i) => {
    if (!within) return true;
    const [node, o] = raw[i];
    if (!node) return true;
    const range = document.createRange();
    range.setStart(node, o);
    range.setEnd(node, o + 1);
    const box = range.getBoundingClientRect();
    if (!box.height) return true;
    const point = viewer.clientPointToPdf(entry, box.left, box.top + box.height / 2);
    return point.y >= within.y - 1 && point.y <= within.y + within.height + 1;
  };

  let start = at;
  while (start > 0 && at - start < MAX_SENTENCE && !stopsAt(raw, start - 1) && inside(start - 1)) {
    start -= 1;
  }
  let end = at;
  while (end < raw.length - 1 && end - at < MAX_SENTENCE && !stopsAt(raw, end) && inside(end + 1)) {
    end += 1;
  }
  if (at - start >= MAX_SENTENCE || end - at >= MAX_SENTENCE) return null;
  while (start < end && (!raw[start][0] || /\s/.test(raw[start][2]))) start += 1;

  const text = raw
    .slice(start, end + 1)
    .map(([, , ch]) => ch)
    .join('')
    .replace(/\s+/g, ' ')
    .trim();
  const rects = lineRects(viewer, entry, raw, start, end);
  if (!rects || text.length < 2) return null;
  return { text, rects };
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
    // (file, line) -> where a note goes on a beamer slide, or null off one.
    // A slide has no margin for a slip and no line positions to level one
    // with, so a note there is a chip at the end of the block it is about.
    this.slideSpots = null;
    this._chips = new Map(); // how many chips already sit at a spot, per draw
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
    this._matches = new Map(); // sentence@page -> its lines in PDF points, or null
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
    this._chips = new Map();
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

  /** A note on a slide: a speech bubble at the end of its block, on every
      page of the slide that shows the block. */
  _drawChips(item, spots, replies, title) {
    const key = `${item.file}:${item.line}`;
    for (const { entry, left, top } of spots) {
      const slot = `${entry.num}:${Math.round(left)}:${Math.round(top)}`;
      const taken = this._chips.get(slot) || 0;
      this._chips.set(slot, taken + 1);

      const node = el('button', `note-chip${item.status === 'done' ? ' done' : ''}`);
      node.type = 'button';
      node.title = title;
      node.dataset.key = key;
      node.innerHTML =
        '<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true"><path fill="currentColor" ' +
        'd="M2 2.5h12v8.5H7.5L4 14v-3H2z"/></svg>';
      if (replies) node.append(el('span', 'note-chip-count', String(replies + 1)));
      // What it says, beside it, when pointed at.
      const said = el('span', 'note-chip-text');
      if (item.by) said.append(el('span', 'note-by', item.by));
      said.append(el('span', '', item.text));
      node.append(said);
      node.style.left = `${left + taken * 24}px`;
      node.style.top = `${top}px`;
      node.addEventListener('click', (event) => {
        event.stopPropagation();
        if (event.altKey || !this.onThread) {
          this.onEdit?.(item.file, item.line);
          return;
        }
        this.onThread(item, this.items, () => this._cardFor(key));
      });
      this._layerFor(entry).append(node);
    }
  }

  _drawNote(item, replies = 0) {
    const who = [item.by, item.re && `\u21b3 ${item.re}`].filter(Boolean).join(' ');
    const done = item.status === 'done';
    const answered = replies ? ` \u00b7 ${replies} repl${replies > 1 ? 'ies' : 'y'}` : '';
    // A marker sits on the line it is about; a %@ line is about the one below.
    const about = item.record ? item.line : (item.end ?? item.line) + 1;
    const spots = this.slideSpots?.(item.file, about);
    if (spots) {
      this._drawChips(
        item,
        spots,
        replies,
        `${item.kind}${who ? ` (${who})` : ''}${item.status ? ` [${item.status}]` : ''}${answered}: ` +
          `${item.text}\n${item.file}:${item.line} — click to open \u00b7 Alt-click to edit`
      );
      return null;
    }
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
      const node = entry.el.querySelector(
        `.note-card[data-key="${CSS.escape(key)}"], .note-chip[data-key="${CSS.escape(key)}"]`
      );
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

  /** Sources by the sentence they back, in source order, each group placed
   * where its sentence ends: its last underline segment, or failing that the
   * line the annotation was placed beside. */
  _sourceGroups() {
    const groups = new Map();
    for (const item of this.sources) {
      const key = `${item.file}#${item.sentence || `a${item.line}`}`;
      const group = groups.get(key);
      if (group) group.items.push(item);
      else groups.set(key, { key, items: [item] });
    }
    for (const group of groups.values()) {
      group.items.sort((a, b) => a.line - b.line);
      const [first] = group.items;
      group.segments = first.segments || [];
      const last = group.segments[group.segments.length - 1];
      group.anchor = last ? { ...first, page: last.page, y: last.top } : first;
    }
    return groups;
  }

  /** Underlines for each sourced sentence, and a mark in the margin for each. */
  _drawSources() {
    const groups = this._sourceGroups();
    const taken = new Map(); // page#top -> marks already on that line
    for (const group of groups.values()) {
      const lines = this._underline(group);
      // Level with the line the sentence ends on: the last underline drawn,
      // or failing that where the build placed the annotation.
      const last = lines[lines.length - 1];
      const entry = last ? last.entry : this.viewer.pageEntry(group.anchor.page);
      if (!entry?.viewport) continue;
      const column = this.viewer.pdfRectToPageRect(entry, {
        x: group.anchor.x,
        y: 0,
        width: Math.max(group.anchor.right - group.anchor.x, 1),
        height: 1,
      });
      const top = last ? last.rect.top : this._place(group.anchor)?.rect.top;
      if (top == null) continue;
      const slot = `${entry.num}#${Math.round(top)}`;
      const nth = taken.get(slot) || 0;
      taken.set(slot, nth + 1);

      const count = group.items.length;
      const [only] = group.items;
      const node = el('button', 'note-source');
      node.type = 'button';
      node.dataset.key = group.key;
      node.append(svgIcon(count > 1 ? 'sources' : only.source.type));
      if (count > 1) node.append(el('span', 'note-source-count', String(count)));
      node.title =
        count > 1
          ? `${count} sources for this sentence \u2014 click to list`
          : `${describeSource(only.source)}\nClick to open \u00b7 Alt-click to edit`;
      node.style.left = `${column.left + column.width + 2 + nth * (SOURCE_GUTTER + 2)}px`;
      node.style.top = `${top + 1}px`;
      node.classList.toggle('open', this.popup?.key === group.key);
      // Pointing at the mark shows which words it is about.
      const nodes = lines.map((line) => line.node);
      node.addEventListener('mouseenter', () => nodes.forEach((line) => line.classList.add('hot')));
      node.addEventListener('mouseleave', () => {
        if (this.popup?.key !== group.key) nodes.forEach((line) => line.classList.remove('hot'));
      });
      node.addEventListener('click', (event) => {
        event.stopPropagation();
        if (count === 1) {
          if (event.altKey) this.onEdit?.(only.file, only.line);
          else this.onPeek?.(only);
          return;
        }
        if (this.popup?.key === group.key) this.closePopup();
        else this._openPopup(group, node, entry);
      });
      this._layerFor(entry).append(node);
    }

    // The open list follows its mark through zooms; it goes if its sentence did.
    if (this.popup) {
      const group = groups.get(this.popup.key);
      const icon = this._iconFor(this.popup.key);
      if (!group) this.closePopup();
      else if (icon) this._positionPopup(icon.node, icon.entry);
    }
  }

  /**
   * A sentence's underline, one piece per rendered line: its own words, found
   * in the page's text, when they can be; else where the build says it is.
   */
  _underline(group) {
    const lines = [];
    const pages = [...new Set(group.segments.map((segment) => segment.page))];
    for (const number of pages) {
      const entry = this.viewer.pageEntry(number);
      if (!entry?.viewport) continue;
      const hints = group.segments.filter((segment) => segment.page === number);
      const found = this._sentenceOn(group, entry, hints);
      const rects = found
        ? found.map((r) => this.viewer.pdfRectToPageRect(entry, r))
        : hints.map((segment) =>
            this.viewer.pdfRectToPageRect(entry, {
              x: segment.x0,
              y: segment.top,
              width: Math.max(segment.x1 - segment.x0, 1),
              height: segment.base - segment.top + 2.5,
            })
          );
      for (const rect of rects) {
        const node = el('div', 'source-underline');
        node.dataset.key = group.key;
        node.style.left = `${rect.left}px`;
        node.style.top = `${rect.top}px`;
        node.style.width = `${rect.width}px`;
        node.style.height = `${rect.height}px`;
        if (this.popup?.key === group.key) node.classList.add('hot');
        this._layerFor(entry).append(node);
        lines.push({ entry, rect, node });
      }
    }
    return lines;
  }

  /**
   * Where a sentence's own words are on a page, as one rectangle per rendered
   * line in PDF points; `null` when they cannot be found (or the page's text is
   * not drawn yet). Found once per page and remembered, so zooming is free.
   */
  _sentenceOn(group, entry, hints) {
    const id = `${group.key}@${entry.num}`;
    if (this._matches.has(id)) return this._matches.get(id);
    const index = textIndex(entry);
    if (!index) return null; // try again when the text is there
    const tokens = texTokens(group.items[0].sentenceText || '');
    const found = tokens.some((t) => t !== GAP)
      ? matchSentence(index, tokens, entry, this.viewer, hints)
      : null;
    this._matches.set(id, found);
    return found;
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
    document
      .querySelectorAll(`.source-underline[data-key="${CSS.escape(group.key)}"]`)
      .forEach((line) => line.classList.add('hot'));
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
    document.querySelectorAll('.source-underline.hot').forEach((line) => line.classList.remove('hot'));
    this.popup = null;
    document.removeEventListener('mousedown', this._closeOnOutside);
    document.removeEventListener('keydown', this._closeOnEscape);
  }

  /** One source in the list: what it links to, and what it says. */
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
