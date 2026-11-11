// Selector icons on the slides of a beamer deck.
//
// SyncTeX cannot see inside a slide: beamer typesets a frame at its
// \end{frame}, so a click anywhere on it resolves to that one line. Pointing at
// a slide therefore goes the other way round. The server reads each frame's
// blocks out of the source — its title, each \item, each paragraph — with the
// lines they occupy (see frames.py and slides.py), and this finds each block's
// words in the page's text and hangs a small icon beside them. An icon stands
// for an exact range of the source; the matching only decides where to draw it,
// so a block whose words cannot be found (a bullet that is all math, an item
// not yet uncovered on this overlay) simply has no icon, and the slide's own
// icon in its corner always covers the whole frame.
//
// There are two ways to point. The icons are always there (unless switched off
// in the View menu). Pick mode is the browser inspector's: switch it on, and
// whatever block is under the pointer lights up — the innermost one, the slide
// itself where there is none — and a click picks it.
//
// Like the paragraph bars, the icons live in an overlay every page render
// wipes: `draw()` reruns on any render or zoom, `refresh()` refetches when the
// document has been rebuilt.

import { getJSON } from './api.js';

const ICON = 18; // px
// A pencil for a block: what an anchor is for is changing it, by asking,
// commenting or typing. A slide's own anchor is a slide.
const PENCIL =
  '<svg viewBox="0 0 16 16" width="11" height="11" aria-hidden="true"><path fill="none" ' +
  'stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" ' +
  'd="M2.5 13.5l.7-3.1 7.6-7.6a1.4 1.4 0 0 1 2 0l.4.4a1.4 1.4 0 0 1 0 2l-7.6 7.6zM9.6 4l2.4 2.4"/></svg>';
const SLIDE =
  '<svg viewBox="0 0 16 16" width="11" height="11" aria-hidden="true"><path fill="none" ' +
  'stroke="currentColor" stroke-width="1.5" stroke-linejoin="round" ' +
  'd="M2 3.5h12v7.5H2zM5.5 14h5M8 11v3"/></svg>';
const ICON_GAP = 6; // px between an icon and the text it is for
const OPENING = 8; // words of a block that are looked for on the page
const LEAD = 3; // words a line may put before them: a label, a symbol
// How far from a block's lines a click still counts as on it, in PDF points:
// some way along the line, into its margin, but hardly at all above or below,
// where the next line is.
const NEAR_X_PT = 24;
const NEAR_Y_PT = 2;

const ANCHORS_KEY = 'texai:slide-anchors';

const TOKEN_RE = /[\p{L}\p{N}]+/gu;

/** The words of a string, folded so `ﬁrst` on the page meets `first` in the source. */
function tokens(text) {
  return (text || '').normalize('NFKC').toLowerCase().match(TOKEN_RE) || [];
}

/** How many of `want`'s leading words open `have`, in order. */
function prefix(want, have, from) {
  let n = 0;
  while (n < want.length && from + n < have.length && have[from + n] === want[n]) n += 1;
  return n;
}

/**
 * How well a rendered line opens a block: 0 when it does not, up to 1.
 *
 * The line may lead with things the source does not spell as words — an
 * enumerate's "1.", a bullet set as a character, the hat and the letter of a
 * $\\hat\\beta$ — so the match may start a few words in, at a small cost: a line
 * that opens with the block's words beats one that merely contains them.
 */
function opens(block, line) {
  const want = block.slice(0, OPENING);
  if (!want.length || !line.length) return { score: 0, label: false };
  let best = { score: 0, label: false };
  for (let from = 0; from <= LEAD && from < line.length; from += 1) {
    const run = prefix(want, line, from);
    // A single word is only a match when it is the whole of what was wanted:
    // one shared "the" does not make a line the start of a bullet.
    if (run < Math.min(2, want.length)) continue;
    const most = Math.min(want.length, line.length - from);
    const score = run / Math.max(most, 1) - 0.05 * from;
    if (score > best.score) best = { score, label: from > 0 };
  }
  return best;
}

/** The share of a line's words that the block also has: is this line more of it? */
function belongs(blockWords, line) {
  if (!line.length) return 0;
  let hits = 0;
  for (const word of line) if (blockWords.has(word)) hits += 1;
  return hits / line.length;
}

const overlapX = (a, b) => Math.min(a.x + a.width, b.x + b.width) - Math.max(a.x, b.x);

export class SlideBlocks {
  /**
   * `onPick(passage, event)` is called when an icon is clicked. The passage is
   * what the ask and comment boxes take — text, page, rectangles — and already
   * carries its `source`: `{ file, line, endLine }`.
   */
  constructor({ viewer, onPick = null, anchorsButton = null, pickButton = null }) {
    this.viewer = viewer;
    this.onPick = onPick;
    this.frames = [];
    this.byPage = new Map(); // page number -> frame
    this.placed = new Map(); // page number -> [{ block, rects }]

    // Both controls only mean something on a deck: hidden until there is one.
    this.anchorsButton = anchorsButton;
    this.pickButton = pickButton;
    this.anchors = localStorage.getItem(ANCHORS_KEY) !== '0';
    this.picking = false;
    this.hover = null; // what pick mode has lit up: { page, key, marks }
    anchorsButton?.addEventListener('click', () => {
      this.anchors = !this.anchors;
      localStorage.setItem(ANCHORS_KEY, this.anchors ? '1' : '0');
      this._reflect();
      this.draw();
    });
    pickButton?.addEventListener('click', () => this.setPicking(!this.picking));
    this._reflect();

    const host = viewer.container;
    host.addEventListener('mousemove', (event) => this.picking && this._hoverAt(event));
    host.addEventListener('mouseleave', () => this._unhover());
    // Ahead of the page's own click handling: in pick mode a click is a pick.
    host.addEventListener(
      'click',
      (event) => {
        if (!this.picking || event.button !== 0) return;
        if (event.target.closest?.('button, a, .ask-box, .comment-box, .inline-edit')) return;
        const at = this._under(event);
        if (!at) return;
        event.preventDefault();
        event.stopPropagation();
        this.onPick?.(this._passage(at.entry, at.source, { ...at.point, width: 0, height: 0 }), event);
      },
      true
    );
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && this.picking && !event.defaultPrevented) this.setPicking(false);
    });
  }

  _reflect() {
    const deck = this.frames.length > 0;
    if (this.anchorsButton) {
      this.anchorsButton.hidden = !deck;
      this.anchorsButton.classList.toggle('active', this.anchors);
      this.anchorsButton.setAttribute('aria-pressed', String(this.anchors));
    }
    if (this.pickButton) {
      this.pickButton.hidden = !deck;
      this.pickButton.classList.toggle('active', this.picking);
      this.pickButton.setAttribute('aria-pressed', String(this.picking));
    }
  }

  /* ---------------- pick mode ---------------- */

  setPicking(on) {
    this.picking = !!on && this.frames.length > 0;
    this.viewer.container.classList.toggle('slide-picking', this.picking);
    if (this.picking) window.getSelection()?.removeAllRanges();
    else this._unhover();
    this._reflect();
  }

  /** The page, point and block under a mouse event, when it is on a slide. */
  _under(event) {
    const entry = this.viewer.pageAtClientPoint(event.clientX, event.clientY);
    if (!entry?.viewport) return null;
    const point = this.viewer.clientPointToPdf(entry, event.clientX, event.clientY);
    const source = this.sourceAt(entry.num, point);
    return source ? { entry, point, source } : null;
  }

  _hoverAt(event) {
    const at = event.target.closest?.('button, a, .ask-box, .comment-box, .inline-edit')
      ? null
      : this._under(event);
    if (!at) {
      this._unhover();
      return;
    }
    const { entry, source } = at;
    const key = `${source.line}-${source.endLine}`;
    const layer = this._layerFor(entry);
    // Unchanged, and still drawn: a page render wipes the layer under us.
    if (this.hover?.page === entry.num && this.hover.key === key && this.hover.layer === layer) return;
    this._unhover();
    const marks = source.rects.map((rect) => {
      const r = this.viewer.pdfRectToPageRect(entry, rect);
      const mark = document.createElement('div');
      mark.className = 'slide-hover';
      Object.assign(mark.style, {
        left: `${r.left - 3}px`,
        top: `${r.top - 2}px`,
        width: `${r.width + 6}px`,
        height: `${r.height + 4}px`,
      });
      layer.append(mark);
      return mark;
    });
    // No block under the pointer: the slide itself is what a click would pick.
    if (!marks.length) entry.el.classList.add('slide-hovered');
    this.hover = { page: entry.num, key, layer, marks, el: entry.el };
  }

  _unhover() {
    if (!this.hover) return;
    for (const mark of this.hover.marks) mark.remove();
    this.hover.el.classList.remove('slide-hovered');
    this.hover = null;
  }

  /** What the ask and comment boxes take, for a block or a slide. */
  _passage(entry, source, anchor, beside = false) {
    const rects = source.rects.length
      ? source.rects
      : [{ page: entry.num, x: 0, y: 12, width: Math.min(44, entry.viewport.viewBox[2]), height: 14 }];
    return {
      text: source.text || (source.kind === 'slide' ? 'this slide' : ''),
      page: entry.num,
      point: { x: rects[0].x + rects[0].width / 2, y: rects[0].y + rects[0].height / 2 },
      word: null,
      rects,
      // Where it was picked: what is offered for it is offered there.
      anchor: { page: entry.num, ...anchor, beside },
      source,
    };
  }

  async refresh() {
    try {
      const data = await getJSON('/api/slides');
      this.frames = data.frames || [];
    } catch {
      // A PDF mid-rebuild: keep what we had; the next build replaces it.
      return;
    }
    this.byPage = new Map();
    for (const frame of this.frames) for (const page of frame.pages) this.byPage.set(page, frame);
    this.placed = new Map();
    this._unhover();
    if (!this.frames.length) this.setPicking(false);
    this._reflect();
    this.draw();
  }

  /** The frame a page shows, or nothing when the page is not a slide. */
  frameOn(page) {
    return this.byPage.get(page) || null;
  }

  /**
   * Where a point on a slide comes from: the block whose lines it is on or
   * beside, else the slide. Nothing when the page is not a slide.
   */
  sourceAt(page, point) {
    const frame = this.frameOn(page);
    if (!frame) return null;
    let best = null;
    let bestDistance = Infinity;
    for (const { block, rects } of this._place(page) || []) {
      for (const r of rects) {
        const dx = Math.max(r.x - point.x, 0, point.x - (r.x + r.width));
        const dy = Math.max(r.y - point.y, 0, point.y - (r.y + r.height));
        // A line that belongs to no block must not be taken for its neighbour:
        // the slide is the honest answer there.
        if (dx > NEAR_X_PT || dy > NEAR_Y_PT) continue;
        const distance = Math.hypot(dx, dy);
        if (distance < bestDistance) {
          best = block;
          bestDistance = distance;
        }
      }
    }
    const placed = best && (this._place(page) || []).find((p) => p.block === best);
    return { ...this._source(frame, best), page, rects: placed ? placed.rects : [] };
  }

  _source(frame, block) {
    return {
      file: frame.file,
      line: block ? block.start : frame.start,
      endLine: block ? block.end : frame.end,
      column: 1,
      word: null,
      kind: block ? block.kind : 'slide',
      text: block ? block.text : frame.title,
    };
  }

  /**
   * Where a note about a source line goes: on each page of its slide that
   * shows the block the line is in, just past the end of that block (and past
   * its anchor), or beside the slide's own icon when the line is in no block.
   * Null when the line is not on a slide, which leaves the note to the margin.
   */
  spotsFor(file, line) {
    const frame = this.frames.find((f) => f.file === file && f.start <= line && line <= f.end);
    if (!frame) return null;
    const holds = (block) => block.start <= line && line <= block.end;
    const inBlock = frame.blocks.some(holds);
    const step = this.anchors ? ICON + 4 : 0;
    const spots = [];
    for (const page of frame.pages) {
      const entry = this.viewer.pageEntry(page);
      if (!entry?.viewport || !entry.rendered) continue;
      if (!inBlock) {
        spots.push({ entry, left: 6 + step, top: 6 });
        continue;
      }
      // The innermost block: a sub-item's note is the sub-item's.
      let best = null;
      for (const placed of this._place(page) || []) {
        if (!holds(placed.block)) continue;
        if (!best || placed.block.end - placed.block.start < best.block.end - best.block.start) {
          best = placed;
        }
      }
      if (!best) continue; // not on this page: an item still covered
      const last = this.viewer.pdfRectToPageRect(entry, best.rects[best.rects.length - 1]);
      spots.push({
        entry,
        left: Math.min(last.left + last.width + ICON_GAP + step, entry.el.clientWidth - ICON - 2),
        top: last.top + (last.height - ICON) / 2,
      });
    }
    return spots;
  }

  /* ---------------- placing ---------------- */

  /**
   * Each block of the page's frame that can be found in its text, with the
   * rendered lines it covers in PDF points. Kept once the text layer is up:
   * PDF points do not move with the zoom.
   */
  _place(page) {
    if (this.placed.has(page)) return this.placed.get(page);
    const frame = this.frameOn(page);
    const entry = this.viewer.pageEntry(page);
    if (!frame || !entry?.viewport || !this.viewer.hasTextLayer(page)) return null;

    const lines = this.viewer.textLines(entry).map((line) => ({ ...line, words: tokens(line.text) }));
    const taken = new Set();
    const found = [];

    for (const block of frame.blocks) {
      const want = tokens(block.text);
      let best = -1;
      let bestScore = 0;
      let label = false;
      lines.forEach((line, i) => {
        if (taken.has(i)) return;
        const { score, label: labelled } = opens(want, line.words);
        // Lines come top to bottom, so of two equal matches the upper one wins:
        // a title repeated in the footline is the footline's, not the title's.
        if (score > bestScore) {
          best = i;
          bestScore = score;
          label = labelled;
        }
      });
      if (best === -1) continue;
      taken.add(best);
      found.push({ block, first: best, words: new Set(want), label });
    }

    // A block runs on from its first line over the lines below that are in its
    // column, belong to no other block, and are made of its words.
    const claimed = new Set(taken);
    const placed = found.map(({ block, first, words, label }) => {
      const rects = [lines[first]];
      for (let i = first + 1; i < lines.length; i += 1) {
        const line = lines[i];
        const last = rects[rects.length - 1];
        if (line.y - (last.y + last.height) > 2 * last.height) break;
        if (overlapX(line, lines[first]) <= 0) continue; // the other column
        if (taken.has(i) || belongs(words, line.words) < 0.6) break;
        rects.push(line);
        claimed.add(i);
      }
      return {
        block,
        // Whether the line already leads with the item's label ("1."), which
        // is then text; a drawn bullet is not, and sits further left.
        label,
        rects: rects.map(({ x, y, width, height }) => ({ page, x, y, width, height })),
      };
    });
    // What is left: blocks with no words to find — a bullet that is all
    // symbols — and lines no block has claimed. An item between two placed
    // blocks is the unclaimed line between them, in order.
    const where = new Map(placed.map((p) => [p.block, p]));
    const filled = [];
    frame.blocks.forEach((block, at) => {
      if (where.has(block) || block.kind !== 'item') return;
      const before = [...frame.blocks.slice(0, at)].reverse().find((b) => where.has(b));
      const after = frame.blocks.slice(at + 1).find((b) => where.has(b));
      if (!before) return;
      const above = where.get(before).rects;
      const top = above[above.length - 1];
      const floor = after ? where.get(after).rects[0].y : top.y + top.height + 2.5 * top.height;
      const i = lines.findIndex(
        (line, n) =>
          !claimed.has(n) &&
          line.y >= top.y + top.height * 0.5 &&
          line.y + line.height <= floor + line.height * 0.5 &&
          line.y - (top.y + top.height) < 2.5 * top.height
      );
      if (i === -1) return;
      claimed.add(i);
      const { x, y, width, height } = lines[i];
      // Its icon lines up with its siblings': labelled as they are.
      const sibling = [before, after].find((b) => b && b.kind === 'item');
      const label = sibling ? where.get(sibling).label : false;
      const entry2 = { block, label, rects: [{ page, x, y, width, height }] };
      where.set(block, entry2);
      filled.push(entry2);
    });
    placed.push(...filled);

    this.placed.set(page, placed);
    return placed;
  }

  /* ---------------- drawing ---------------- */

  clear() {
    for (const entry of this.viewer.pages) entry.el.querySelector('.slide-layer')?.remove();
  }

  _layerFor(entry) {
    let layer = entry.el.querySelector('.slide-layer');
    if (!layer) {
      layer = document.createElement('div');
      layer.className = 'slide-layer';
      entry.el.append(layer);
    }
    return layer;
  }

  /** Re-place every icon. Cheap enough to run on any render or zoom. */
  draw() {
    this.clear();
    if (!this.byPage.size || !this.anchors) return;

    for (const entry of this.viewer.pages) {
      const frame = this.frameOn(entry.num);
      if (!frame || !entry.viewport || !entry.rendered) continue;
      const layer = this._layerFor(entry);

      // The slide itself, in its corner, whether or not its text is up yet.
      const whole = { page: entry.num, x: 0, y: 0, width: entry.viewport.viewBox[2], height: 0 };
      this._icon(layer, entry, frame, null, [whole], { left: 6, top: 6 });

      for (const { block, rects } of this._place(entry.num) || []) {
        // At the end of the block's text, where a hand finishes reading it and
        // clear of bullets and labels; kept on the page when the line is full.
        const last = this.viewer.pdfRectToPageRect(entry, rects[rects.length - 1]);
        this._icon(layer, entry, frame, block, rects, {
          left: Math.min(last.left + last.width + ICON_GAP, entry.el.clientWidth - ICON - 2),
          top: last.top + (last.height - ICON) / 2,
        });
      }
    }
  }

  _icon(layer, entry, frame, block, rects, at) {
    const source = this._source(frame, block);
    const node = document.createElement('button');
    node.type = 'button';
    node.className = `slide-icon ${source.kind}`;
    node.innerHTML = block ? PENCIL : SLIDE;
    node.style.left = `${at.left}px`;
    node.style.top = `${at.top}px`;
    const what = { slide: 'This slide', title: 'The title', item: 'This item', text: 'This paragraph' };
    const where =
      source.endLine > source.line ? `${source.line}-${source.endLine}` : `${source.line}`;
    node.title = `${what[source.kind] || 'This block'} — ${source.file}:${where}`;
    node.setAttribute('aria-label', node.title);

    // Hovering shows what the icon stands for, before anything is asked of it.
    const marks = [];
    node.addEventListener('mouseenter', () => {
      for (const rect of block ? rects : []) {
        const r = this.viewer.pdfRectToPageRect(entry, rect);
        const mark = document.createElement('div');
        mark.className = 'slide-hover';
        Object.assign(mark.style, {
          left: `${r.left}px`,
          top: `${r.top}px`,
          width: `${r.width}px`,
          height: `${r.height}px`,
        });
        layer.append(mark);
        marks.push(mark);
      }
      if (!block) entry.el.classList.add('slide-hovered');
    });
    node.addEventListener('mouseleave', () => {
      while (marks.length) marks.pop().remove();
      entry.el.classList.remove('slide-hovered');
    });
    // A press must not start a text selection, or clear one.
    node.addEventListener('mousedown', (event) => event.preventDefault());
    node.addEventListener('click', (event) => {
      event.preventDefault();
      event.stopPropagation();
      const picked = { ...source, page: entry.num, rects: block ? rects : [] };
      const at = this.viewer.clientRectToPdf(entry, node.getBoundingClientRect());
      // What an icon offers goes beside it, on its own line: past the end of
      // the text there is room, and above it is somebody else's line.
      this.onPick?.(this._passage(entry, picked, at, true), event);
    });
    layer.append(node);
  }
}
