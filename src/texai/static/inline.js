// Editing a paragraph where it sits on the page.
//
// Double-click a word in the PDF and a small LaTeX editor opens over the
// paragraph it came from, set in a serif at the page's own size and as wide as
// its text column, with the clicked word selected. What it holds is the source
// — commands, math and all — so there is no translation to lose anything in;
// it only looks like the page. Closing it writes that one paragraph back and
// rebuilds, and the box stays up, dimmed, until the new PDF has replaced the
// text underneath it.
//
// The paragraph is the editor's: a blank-line-separated block, the one wearing
// the same colour in the gutter and the margin. The server checks it is still
// there when the edit lands, so an agent writing elsewhere in the same file
// does not cost you the edit, and one rewriting this paragraph does not lose
// its work to yours.
//
// The box lives in the scroll container, not on a page: pages throw away
// everything overlaid on them whenever they re-render, which would take the
// editor and your typing with it.

import { getJSON, postJSON } from './api.js';
import { micButton, stopDictation } from './speech.js';
import { sentenceAt } from './annotations.js';

const FALLBACK_FONT_PT = 10;
const MIN_FONT_PT = 6;
const MAX_FONT_PT = 24;
const PAD_PX = 6; // the box reaches this far past the text column, so the text inside lines up with it
// A saved box waits this long for the rebuilt PDF to arrive before closing anyway.
const SETTLE_MS = 6000;

// How much the box opens on. A long paragraph is a lot of LaTeX to be handed
// for the sake of one word, so past these sizes it opens on the sentence that
// was clicked; the bar widens it to the paragraph, or narrows it to the line.
const SCOPE_KEY = 'texai:inline-scope';
const SCOPES = [
  { key: 'line', label: 'line', title: 'Only the source line that was clicked' },
  { key: 'sentence', label: 'sentence', title: 'The sentence that was clicked: its lines in the source' },
  { key: 'paragraph', label: 'paragraph', title: 'The whole paragraph' },
];
const LOCAL_LINES = 5;
const LOCAL_CHARS = 500;

// What is in the source and not on the page, folded away to a small chip
// unless asked for: texai's own annotations, and LaTeX comments.
const HIDDEN = [
  { kind: 'notes', key: 'texai:inline-show-notes', label: '%@', title: "texai's annotations: %@ lines and %# markers" },
  { kind: 'comments', key: 'texai:inline-show-comments', label: '%', title: 'LaTeX comments' },
];

/** Lines `first`-`last` of a paragraph, cut down to characters [from, to) of its text. */
function piece(para, from, to) {
  const text = para.text;
  const lineStart = text.lastIndexOf('\n', from - 1) + 1;
  const next = text.indexOf('\n', Math.max(from, to - 1));
  const lineEnd = next === -1 ? text.length : next;
  return {
    start: para.start + text.slice(0, from).split('\n').length - 1,
    end: para.start + text.slice(0, Math.max(from, to - 1)).split('\n').length - 1,
    // What shares those lines and stays as it is: written back around the edit.
    head: text.slice(lineStart, from),
    tail: text.slice(to, lineEnd),
    text: text.slice(from, to),
  };
}

/** The characters of line `index` (from 0) of a text. */
function lineSpan(text, index) {
  let from = 0;
  for (let i = 0; i < index; i++) from = text.indexOf('\n', from) + 1;
  const next = text.indexOf('\n', from);
  return [from, next === -1 ? text.length : next];
}

const SENTENCE_STOP = /[.!?]["'”’)}\]]*(?=\s|$)/g;
// A full stop that ends an abbreviation, not a sentence.
const ABBREVIATION = /(?:\b(?:e\.g|i\.e|cf|vs|etc|al|Fig|Eq|Sec|Tab|resp|approx)|\b[A-Z])\.$/;

/**
 * The characters of the sentence at column `ch` of line `index` in a
 * paragraph's source: from the end of the sentence before it to its own full
 * stop, with a `%#` marker that follows on the same line. A comment line is a
 * wall on either side: an annotation belongs to what it is written above.
 */
export function sentenceSpan(text, index, ch) {
  const [lineFrom, lineTo] = lineSpan(text, index);
  const at = Math.min(lineTo, lineFrom + Math.max(0, ch));
  // Walls: the stretches no sentence runs across.
  let low = 0;
  let high = text.length;
  let offset = 0;
  for (const line of text.split('\n')) {
    const end = offset + line.length;
    if (/^\s*%/.test(line)) {
      if (end < at) low = Math.min(text.length, end + 1);
      else if (offset > at) high = Math.min(high, Math.max(0, offset - 1));
      else return [offset, end]; // the click is on the comment itself
    }
    offset = end + 1;
  }
  let from = low;
  let to = high;
  for (const stop of text.slice(low, high).matchAll(SENTENCE_STOP)) {
    const end = low + stop.index + stop[0].length;
    if (ABBREVIATION.test(text.slice(low, low + stop.index + 1))) continue;
    if (end <= at) from = end;
    else {
      to = end;
      break;
    }
  }
  while (from < to && /\s/.test(text[from])) from++;
  const marker = /^[ \t]*%#\S+/.exec(text.slice(to, high));
  if (marker) to += marker[0].length;
  return [from, Math.max(from, to)];
}

/** Where a line's comment starts (its first unescaped %), or -1. */
function commentAt(text) {
  for (let i = text.indexOf('%'); i !== -1; i = text.indexOf('%', i + 1)) {
    let slashes = 0;
    while (i - slashes > 0 && text[i - slashes - 1] === '\\') slashes++;
    if (slashes % 2 === 0) return i;
  }
  return -1;
}

/**
 * The stretches of a piece of LaTeX that never reach the page, as
 * `{ kind, from, to }` in CodeMirror positions: runs of comment-only lines
 * (taken with their line breaks, so no blank is left behind) and comments at
 * the end of a line of text. `kind` is 'notes' for what texai writes (`%@`,
 * its `%>` continuations, `%#` markers) and 'comments' for the rest.
 */
export function unrendered(text) {
  const lines = text.split('\n');
  const found = [];
  let run = null;
  const close = () => {
    if (!run) return;
    const { kind, first, last } = run;
    run = null;
    if (last < lines.length - 1) {
      found.push({ kind, from: { line: first, ch: 0 }, to: { line: last + 1, ch: 0 } });
    } else if (first > 0) {
      // Nothing after it to pull up: fold it into the end of the line before.
      found.push({
        kind,
        from: { line: first - 1, ch: lines[first - 1].length },
        to: { line: last, ch: lines[last].length },
      });
    }
    // All there is: leave it showing, or the box would look empty.
  };
  lines.forEach((line, i) => {
    const at = commentAt(line);
    if (at !== -1 && !line.slice(0, at).trim()) {
      const kind = /^%[@>#]/.test(line.slice(at)) ? 'notes' : 'comments';
      if (run && run.kind === kind) run.last = i;
      else {
        close();
        run = { kind, first: i, last: i };
      }
      return;
    }
    close();
    // A bare % only joins two lines; there is nothing in it to hide.
    if (at === -1 || line.slice(at).trim() === '%') return;
    const kind = /^%#/.test(line.slice(at)) ? 'notes' : 'comments';
    const ch = line.slice(0, at).trimEnd().length;
    found.push({ kind, from: { line: i, ch }, to: { line: i, ch: line.length } });
  });
  close();
  return found;
}

const IS_MAC = navigator.platform.toUpperCase().includes('MAC');

/**
 * What is wrong with the environments in a piece of LaTeX, in words, or null
 * when every \begin has its \end. Comments are skipped; verbatim is not known
 * about, which a block that was whole to start with rarely minds.
 */
function unclosed(text) {
  const open = [];
  const code = text.replace(/(^|[^\\])%.*$/gm, '$1');
  for (const found of code.matchAll(/\\(begin|end)\s*\{([^}]*)\}/g)) {
    const [, kind, name] = found;
    if (kind === 'begin') open.push(name);
    else if (open.pop() !== name) return `\\end{${name}} has no \\begin{${name}} before it`;
  }
  return open.length ? `\\begin{${open.pop()}} is not closed — add its \\end` : null;
}

/**
 * Where a word seen on the page sits in a block's source, or -1. The source
 * spells more than the page shows, so of several occurrences the one with the
 * most of the page's neighbouring words around it wins, and the first on a tie.
 */
function findWord(text, { word, before = [], after = [] }) {
  if (!word) return -1;
  const edge = /[\p{L}\p{N}]/u;
  const reach = 80;
  let best = -1;
  let bestScore = -1;
  for (let at = text.indexOf(word); at !== -1; at = text.indexOf(word, at + 1)) {
    const end = at + word.length;
    // Not inside a longer word, and not the name of a command.
    if (at > 0 && (edge.test(text[at - 1]) || text[at - 1] === '\\')) continue;
    if (end < text.length && edge.test(text[end])) continue;
    const left = text.slice(Math.max(0, at - reach), at);
    const right = text.slice(end, end + reach);
    const score =
      before.filter((w) => left.includes(w)).length + after.filter((w) => right.includes(w)).length;
    if (score > bestScore) {
      best = at;
      bestScore = score;
    }
  }
  return best;
}

export class InlineEditor {
  /**
   * `resolve(entry, clientX, clientY)` is the same lookup Alt-click makes; it
   * answers with the source file, line and (when it can) column. `bars()` is
   * the paragraph guide's current answer, used to fit the box to the paragraph.
   * `refuse(file)` gives a reason not to open on a file, or nothing.
   */
  constructor({ viewer, container, resolve, bars, refuse = () => null, onSaved = null, onError = null }) {
    this.viewer = viewer;
    this.container = container;
    this.resolve = resolve;
    this.bars = bars;
    this.refuse = refuse;
    this.onSaved = onSaved;
    this.onError = onError;
    this.session = null;
    this._opening = 0;

    // Clicking anywhere else is "done". The scroll container itself is left
    // out: that is its scrollbar, or the grey between pages, and reaching for
    // either while typing is not a request to save.
    document.addEventListener(
      'mousedown',
      (event) => {
        const s = this.session;
        if (!s || s.state !== 'editing') return;
        if (s.el.contains(event.target) || event.target === this.container) return;
        this.commit();
      },
      true
    );
  }

  get isOpen() {
    return !!this.session;
  }

  /** Open on the paragraph behind a point on a page. */
  async open(entry, clientX, clientY, target = null) {
    if (this.session) {
      if (this.session.state !== 'editing') {
        this.onError?.('Still saving the last edit…');
        return;
      }
      await this.commit();
      if (this.session) return; // that save was refused; its box keeps the floor
    }

    const token = ++this._opening;
    const fontPt = this._fontAt(target);
    let where;
    let para;
    try {
      where = await this.resolve(entry, clientX, clientY);
      const refusal = this.refuse(where.file);
      if (refusal) throw new Error(refusal);
      para = await getJSON(
        `/api/paragraph?file=${encodeURIComponent(where.file)}&line=${encodeURIComponent(where.line)}`
      );
    } catch (err) {
      if (token === this._opening) this.onError?.(err.message || String(err));
      return;
    }
    if (token !== this._opening || this.session) return;

    const click = this.viewer.clientPointToPdf(entry, clientX, clientY);
    const paraBox = this._boxFor(entry, para, click, fontPt);
    // The pieces the box can be opened on, innermost first, each as the lines
    // it is in and what of those lines lies outside it.
    const at = Math.min(para.end, Math.max(para.start, where.line));
    const pieces = {
      paragraph: piece(para, 0, para.text.length),
      line: piece(para, ...lineSpan(para.text, at - para.start)),
      sentence: piece(para, ...sentenceSpan(para.text, at - para.start, where.word ? where.column - 1 : 0)),
    };
    // Where each sits: the column is the paragraph's, the height its own.
    const near = { ...paraBox, y: Math.max(paraBox.y, click.y - fontPt), height: fontPt * 1.4 };
    const boxes = { paragraph: paraBox, line: near, sentence: near };
    const drawn = sentenceAt(this.viewer, entry, clientX, clientY, paraBox);
    if (drawn?.rects?.length) {
      const top = Math.min(...drawn.rects.map((r) => r.y));
      const bottom = Math.max(...drawn.rects.map((r) => r.y + r.height));
      boxes.sentence = { ...paraBox, y: top, height: bottom - top };
    }

    const stored = localStorage.getItem(SCOPE_KEY);
    const long = para.end - para.start + 1 > LOCAL_LINES || para.text.length > LOCAL_CHARS;
    const scope = SCOPES.some((s) => s.key === stored) ? stored : long ? 'sentence' : 'paragraph';
    const shown = pieces[scope];

    this.session = {
      file: para.file,
      ...shown,
      original: shown.text,
      page: entry.num,
      box: boxes[scope],
      fontPt,
      scope,
      pieces,
      boxes,
      state: 'editing',
      reloaded: false,
      settle: null,
      hidden: [],
      ...this._build({ ...para, ...shown }, { scoped: true }),
    };
    this._setStatus(this._where(this.session), '');
    this._showScope();
    this._fold();
    this.place();
    this._putCursor(where);
  }

  /** Open the box on more, or less, of the passage: its line, sentence or paragraph. */
  rescope(scope) {
    const s = this.session;
    if (!s?.pieces || s.state !== 'editing' || scope === s.scope) return;
    localStorage.setItem(SCOPE_KEY, scope);
    if (s.saved || s.cm.getValue() !== s.original) {
      // The edit belongs to the lines it was made in.
      this._setStatus('Save or cancel this edit first; the choice is kept for the next one.', 'err');
      return;
    }
    const shown = s.pieces[scope];
    Object.assign(s, shown, { original: shown.text, scope, box: s.boxes[scope] });
    s.cm.setValue(shown.text);
    s.cm.clearHistory();
    this._setStatus(this._where(s), '');
    this._showScope();
    this._fold();
    this.place();
    s.cm.focus();
  }

  _where(s) {
    const mod = IS_MAC ? '⌘' : 'Ctrl';
    const lines = s.end > s.start ? `${s.start}-${s.end}` : `${s.start}`;
    return `${s.file}:${lines} · ${mod}-Enter or click away to save · Esc to cancel`;
  }

  _showScope() {
    const s = this.session;
    for (const button of s.scopes?.children || []) {
      button.setAttribute('aria-checked', String(button.dataset.key === s.scope));
    }
  }

  /**
   * Fold away what the page does not show, down to a chip each: the text is
   * still in the box, and saved with it, only out of the way of the prose. A
   * click on a chip opens that one; the bar's toggles show or hide them all.
   */
  _fold() {
    const s = this.session;
    if (!s) return;
    for (const mark of s.hidden) mark.clear();
    s.hidden = [];
    const found = unrendered(s.cm.getValue());
    for (const { kind, key, label } of HIDDEN) {
      const toggle = s.toggles[kind];
      const shown = localStorage.getItem(key) === '1';
      toggle.hidden = !found.some((f) => f.kind === kind);
      toggle.setAttribute('aria-pressed', String(shown));
      if (shown) continue;
      for (const { from, to } of found.filter((f) => f.kind === kind)) {
        const chip = document.createElement('span');
        chip.className = `inline-folded ${kind}`;
        chip.textContent = label;
        chip.title = `${s.cm.getRange(from, to).trim()}\n(click to show)`;
        const mark = s.cm.markText(from, to, { replacedWith: chip, atomic: true });
        chip.addEventListener('mousedown', (event) => {
          event.preventDefault();
          mark.clear();
        });
        s.hidden.push(mark);
      }
    }
    s.cm.refresh();
  }

  /**
   * Open on a block the source names outright: an item, a title or a paragraph
   * of a beamer slide, or the whole frame. `source` is `{ file, line, endLine }`
   * and `rects` where it sits on `page`, in PDF points (none for a frame).
   * `word`, when the box was opened by a double-click, is the word under it
   * with its neighbours (`{ word, before, after }`), to be selected in the box.
   */
  async openBlock(source, page, rects = [], word = null) {
    if (this.session) {
      if (this.session.state !== 'editing') {
        this.onError?.('Still saving the last edit…');
        return;
      }
      await this.commit();
      if (this.session) return;
    }
    const entry = this.viewer.pageEntry(page);
    if (!entry?.viewport) return;

    const token = ++this._opening;
    let para;
    try {
      const refusal = this.refuse(source.file);
      if (refusal) throw new Error(refusal);
      para = await getJSON(
        `/api/paragraph?file=${encodeURIComponent(source.file)}` +
          `&line=${source.line}&end=${source.endLine ?? source.line}`
      );
    } catch (err) {
      if (token === this._opening) this.onError?.(err.message || String(err));
      return;
    }
    if (token !== this._opening || this.session) return;

    const [x0, y0, x1, y1] = entry.viewport.viewBox;
    const pageWidth = x1 - x0;
    const pageHeight = y1 - y0;
    let box;
    let fontPt;
    if (rects.length) {
      const left = Math.min(...rects.map((r) => r.x));
      const top = Math.min(...rects.map((r) => r.y));
      const right = Math.max(...rects.map((r) => r.x + r.width));
      const bottom = Math.max(...rects.map((r) => r.y + r.height));
      // As wide as the text, and wide enough to type LaTeX in, short of the edge.
      const room = pageWidth - left - 12;
      box = {
        x: left,
        y: top,
        width: Math.min(room, Math.max(right - left + 8, pageWidth * 0.5)),
        height: bottom - top,
      };
      fontPt = rects[0].height * 0.75;
    } else {
      // A whole frame: over the body of the slide, under its title.
      box = { x: pageWidth * 0.06, y: pageHeight * 0.2, width: pageWidth * 0.88, height: pageHeight * 0.3 };
      fontPt = 8;
    }
    fontPt = Math.min(MAX_FONT_PT, Math.max(MIN_FONT_PT, fontPt));

    this.session = {
      file: para.file,
      start: para.start,
      end: para.end,
      original: para.text,
      page,
      box,
      fontPt,
      // A block of a slide was whole when it opened, and is saved whole.
      whole: !unclosed(para.text),
      state: 'editing',
      reloaded: false,
      settle: null,
      hidden: [],
      ...this._build(para),
    };
    this._fold();
    this.place();
    const { cm } = this.session;
    cm.focus();
    const at = word ? findWord(para.text, word) : -1;
    if (at === -1) {
      cm.setCursor(0, cm.getLine(0).length);
    } else {
      // Selected, as a double-click selects a word: typing replaces it.
      cm.setSelection(cm.posFromIndex(at), cm.posFromIndex(at + word.word.length));
    }
  }

  /* ---------------- geometry ---------------- */

  /** The size of the text that was clicked, in points, so the box can match it. */
  _fontAt(target) {
    const span = target?.closest?.('.textLayer span');
    const px = span ? parseFloat(getComputedStyle(span).fontSize) : NaN;
    const pt = px / this.viewer.scale;
    if (!Number.isFinite(pt) || pt <= 0) return FALLBACK_FONT_PT;
    return Math.min(MAX_FONT_PT, Math.max(MIN_FONT_PT, pt));
  }

  /**
   * Where the paragraph sits on this page, in PDF points: its bar's extent and
   * the text column's width. Without a bar (the guide has not loaded, or the
   * paragraph's lines carry no SyncTeX records) a column-ish box at the click
   * is the honest fallback.
   */
  _boxFor(entry, para, click, fontPt) {
    const bar = (this.bars() || []).find(
      (b) => b.file === para.file && b.index === para.index && b.page === entry.num
    );
    if (bar?.width) return { x: bar.x, y: bar.y, width: bar.width, height: bar.height };

    const [x0, , x1] = entry.viewport.viewBox;
    const pageWidth = x1 - x0;
    return {
      x: pageWidth * 0.15,
      y: Math.max(0, click.y - fontPt * 1.5),
      width: pageWidth * 0.7,
      height: fontPt * 3,
    };
  }

  /** Re-place the box over its paragraph. Cheap; runs on every render and zoom. */
  place() {
    const s = this.session;
    if (!s) return;
    const entry = this.viewer.pageEntry(s.page);
    if (!entry?.viewport) return;

    const rect = this.viewer.pdfRectToPageRect(entry, s.box);
    const { left, top } = this.viewer.pageRectToContainer(entry, rect);

    const { style } = s.el;
    style.left = `${left - PAD_PX}px`;
    style.top = `${top - PAD_PX}px`;
    style.width = `${rect.width + 2 * PAD_PX}px`;
    style.minHeight = `${rect.height + 2 * PAD_PX}px`;

    const fontPx = s.fontPt * this.viewer.scale;
    if (fontPx !== s.fontPx) {
      s.fontPx = fontPx;
      style.setProperty('--inline-font', `${fontPx}px`);
      s.cm.refresh();
    }
  }

  /* ---------------- the box ---------------- */

  _build(para, { scoped = false } = {}) {
    const el = document.createElement('div');
    el.className = 'inline-edit';

    const host = document.createElement('div');
    host.className = 'inline-edit-host';

    const bar = document.createElement('div');
    bar.className = 'inline-edit-bar';
    const status = document.createElement('span');
    status.className = 'inline-edit-status';
    const mod = IS_MAC ? '⌘' : 'Ctrl';
    status.textContent = `${para.file}:${para.start} · ${mod}-Enter or click away to save · Esc to cancel`;
    status.title = status.textContent;
    const cancel = document.createElement('button');
    cancel.type = 'button';
    cancel.textContent = 'Cancel';
    cancel.addEventListener('click', () => this.cancel());
    const save = document.createElement('button');
    save.type = 'button';
    save.className = 'primary';
    save.textContent = 'Save';
    save.addEventListener('click', () => this.commit());
    bar.append(status);
    // How much of the passage: only where the source leaves a choice (a block
    // of a slide is the block).
    let scopes = null;
    if (scoped) {
      scopes = document.createElement('div');
      scopes.className = 'inline-edit-switch';
      scopes.setAttribute('role', 'radiogroup');
      scopes.setAttribute('aria-label', 'How much to edit');
      for (const { key, label, title } of SCOPES) {
        const button = document.createElement('button');
        button.type = 'button';
        button.textContent = label;
        button.title = title;
        button.dataset.key = key;
        button.setAttribute('role', 'radio');
        button.addEventListener('click', () => this.rescope(key));
        scopes.append(button);
      }
      bar.append(scopes);
    }
    const toggles = {};
    for (const { kind, key, label, title } of HIDDEN) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'inline-edit-fold';
      button.textContent = label;
      button.title = `Show or hide ${title}`;
      button.addEventListener('click', () => {
        localStorage.setItem(key, localStorage.getItem(key) === '1' ? '0' : '1');
        this._fold();
        this.session?.cm.focus();
      });
      toggles[kind] = button;
      bar.append(button);
    }
    bar.append(cancel, save);

    el.append(host, bar);
    this.container.append(el);

    const cm = window.CodeMirror(host, {
      value: para.text,
      mode: 'stex',
      lineWrapping: true,
      matchBrackets: true,
      // Grow with the text rather than scroll inside a box of its own.
      viewportMargin: Infinity,
      extraKeys: {
        Esc: () => this.cancel(),
        'Cmd-Enter': () => this.commit(),
        'Ctrl-Enter': () => this.commit(),
        'Cmd-S': () => this.commit(),
        'Ctrl-S': () => this.commit(),
      },
    });
    // Dictate into the box: words go in at the caret, as LaTeX's plain text.
    const mic = micButton({ cm, onError: (message) => this._setStatus(message, 'err') });
    if (mic) cancel.before(mic);
    return { el, cm, status, save, scopes, toggles };
  }

  /** The caret where the double-click was, with the word selected as a double-click selects it. */
  _putCursor(where) {
    const { cm, start, end } = this.session;
    cm.focus();
    if (where.line < start || where.line > end) {
      cm.setCursor(cm.lastLine(), cm.getLine(cm.lastLine()).length);
      return;
    }
    const line = where.line - start;
    const text = cm.getLine(line) || '';
    // The box may begin partway along its first line.
    const lead = line === 0 ? (this.session.head || '').length : 0;
    const ch = where.word ? Math.max(0, Math.min(text.length, where.column - 1 - lead)) : 0;
    const match = where.word ? /^[\p{L}\p{N}][\p{L}\p{N}'-]*/u.exec(text.slice(ch)) : null;
    if (match) cm.setSelection({ line, ch }, { line, ch: ch + match[0].length });
    else cm.setCursor({ line, ch });
  }

  _setStatus(text, kind = '') {
    const s = this.session;
    if (!s) return;
    s.status.textContent = text;
    s.status.className = `inline-edit-status ${kind}`;
  }

  _setState(state) {
    const s = this.session;
    s.state = state;
    s.el.classList.toggle('saving', state !== 'editing');
    s.cm.setOption('readOnly', state === 'editing' ? false : 'nocursor');
    s.save.disabled = state !== 'editing';
  }

  /* ---------------- closing ---------------- */

  /** Write the paragraph back and rebuild; or just close, if nothing changed. */
  async commit() {
    const s = this.session;
    if (!s || s.state !== 'editing') return;
    stopDictation();
    const text = s.cm.getValue();
    if (text === s.original) {
      this.close();
      return;
    }
    // On a slide a stray \begin or \end breaks the whole frame, not a
    // paragraph: say which one before anything is written.
    const broken = s.whole ? unclosed(text) : null;
    if (broken) {
      this._setStatus(broken, 'err');
      s.cm.focus();
      return;
    }

    this._setState('saving');
    this._setStatus('Saving and rebuilding…', 'busy');
    let result;
    try {
      const head = s.head || '';
      const tail = s.tail || '';
      result = await postJSON('/api/paragraph', {
        file: s.file,
        start: s.start,
        end: s.end,
        // Whole lines travel; a sentence goes back between its neighbours.
        original: head + s.original + tail,
        text: head + text + tail,
      });
    } catch (err) {
      if (this.session !== s) return;
      this._setState('editing');
      this._setStatus(err.message || String(err), 'err');
      s.cm.focus();
      return;
    }
    if (this.session !== s) return;
    this.onSaved?.(result);
    if (!result.changed) {
      this.close();
      return;
    }

    // From here on the paragraph on disk is the new one, whatever the build did.
    s.start = result.start;
    s.end = result.end;
    s.original = s.head || s.tail ? text : result.text;
    s.saved = true;

    const build = result.build || {};
    if (!build.ok) {
      // Saved, but the page cannot show it. Keep the box open on the new text,
      // with LaTeX's complaint, so the fix is one edit away.
      this._setState('editing');
      const first = (build.errors || [])[0];
      this._setStatus(
        `Saved, but the build failed: ${first || build.summary || 'see the log'}`,
        'err'
      );
      s.cm.focus();
      return;
    }

    this._setState('saved');
    this._setStatus('Rebuilt — updating the page…', 'busy');
    // The watcher may already have picked up the new PDF while the build ran.
    if (s.reloaded) this.close();
    else s.settle = setTimeout(() => this.session === s && this.close(), SETTLE_MS);
  }

  /** Throw the edit away; asks first if there is anything to lose. */
  cancel() {
    const s = this.session;
    if (!s || s.state !== 'editing') return;
    if (s.cm.getValue() !== s.original && !window.confirm('Discard your edit to this paragraph?')) {
      s.cm.focus();
      return;
    }
    this.close();
  }

  close() {
    const s = this.session;
    if (!s) return;
    stopDictation();
    clearTimeout(s.settle);
    s.el.remove();
    this.session = null;
  }

  /** A new PDF is on the page: a saved box has done its job. */
  pdfReloaded() {
    const s = this.session;
    if (!s) return;
    if (s.state === 'saved') this.close();
    else if (s.state === 'saving') s.reloaded = true;
    else this.place();
  }
}
