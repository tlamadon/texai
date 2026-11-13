// Pick mode: point at a piece of the document the way a browser's inspector
// points at an element.
//
// Switched on, whatever is under the pointer lights up — the innermost piece
// first — and a click (or a tap: on a touch screen there is no pointer to
// hover with, so the tap does both) picks it, with what can be done to it
// offered right there. Picking the same piece again widens it: a sentence to
// its paragraph, a block of a slide to its slide.
//
// What a "piece" is depends on the page:
//
// * On a beamer slide it is a block of the frame — title, item, paragraph — or
//   the slide. The source says what those are (see slides.js); nothing is asked
//   of the server.
// * On a paper it is a sentence, then its paragraph. The sentence is read off
//   the page's own text, from the full stop before it to its own, so it is
//   exact however the source wraps its lines. The source lines it stands for
//   come from the server: the click is resolved through SyncTeX to a line, and
//   the line to its sentence and paragraph (see /api/block). A paper wrapped at
//   a column has no line per sentence, so there a sentence travels as its
//   paragraph's lines plus its own words — which is how a selection travels.
//
// Selecting text still works with Pick off; with it on, a press picks instead.

import { getJSON, postJSON } from './api.js';
import { sentenceAt } from './annotations.js';
import { passageFor } from './slides.js';

const PICK_KEY = 'texai:pick';
const HOVER_MS = 60; // a pointer at rest this long is asking what is under it
// What is not the page: its own controls, and the boxes that open on it.
const NOT_PAGE = 'button, a, .ask-box, .comment-box, .inline-edit, .pick-bar';

/** Whether this is a device to tap on rather than point at. */
export const touchFirst = () => window.matchMedia?.('(pointer: coarse)').matches ?? false;

export class Picker {
  /**
   * `slides` answers for slide pages; `bars()` is the paragraph guide's list,
   * for which paragraph a point on a paper is in; `onPick(passage, event)`
   * takes what was picked; `onError(message)` hears why a pick found nothing.
   */
  constructor({ viewer, slides, button = null, bars = () => [], onPick = null, onError = null }) {
    this.viewer = viewer;
    this.slides = slides;
    this.button = button;
    this.bars = bars;
    this.onPick = onPick;
    this.onError = onError;
    this.picking = false;
    this.hover = null; // what is lit: { key, nodes, page }
    this.last = null; // the last pick: { key, wider }
    this._timer = null;
    this._asking = 0;

    button?.addEventListener('click', () => this.setPicking(!this.picking));
    const host = viewer.container;
    host.addEventListener('mousemove', (event) => this.picking && this._moved(event));
    host.addEventListener('mouseleave', () => this._unhover());
    // A press in Pick mode is a pick, not the start of a text selection.
    host.addEventListener('mousedown', (event) => {
      if (this.picking && event.button === 0 && !event.target.closest?.(NOT_PAGE)) {
        event.preventDefault();
      }
    });
    // Ahead of the page's own click handling.
    host.addEventListener(
      'click',
      (event) => {
        if (!this.picking || event.button !== 0 || event.target.closest?.(NOT_PAGE)) return;
        const entry = this.viewer.pageAtClientPoint(event.clientX, event.clientY);
        if (!entry?.viewport) return;
        event.preventDefault();
        event.stopPropagation();
        this._pick(entry, event);
      },
      true
    );
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && this.picking && !event.defaultPrevented && !touchFirst()) {
        this.setPicking(false);
      }
    });

    // On a touch screen picking is the way to point at all, so it starts on.
    const stored = localStorage.getItem(PICK_KEY);
    this.setPicking(stored === null ? touchFirst() : stored === '1', { remember: false });
  }

  setPicking(on, { remember = true } = {}) {
    this.picking = !!on;
    this.viewer.container.classList.toggle('picking', this.picking);
    if (this.picking) window.getSelection()?.removeAllRanges();
    else this._unhover();
    this.last = null;
    if (remember) localStorage.setItem(PICK_KEY, this.picking ? '1' : '0');
    if (this.button) {
      this.button.classList.toggle('active', this.picking);
      this.button.setAttribute('aria-pressed', String(this.picking));
    }
  }

  /** A new PDF: nothing lit or picked is where it was. */
  pdfReloaded() {
    this._unhover();
    this.last = null;
  }

  /* ---------------- what is under a point ---------------- */

  /** The paragraph bar a point on a paper page is in, if the guide knows one. */
  _paragraphAt(page, point) {
    let best = null;
    for (const bar of this.bars() || []) {
      if (bar.page !== page || !bar.width) continue;
      if (point.y < bar.y - 1 || point.y > bar.y + bar.height + 1) continue;
      if (point.x < bar.x - 12 || point.x > bar.x + bar.width + 12) continue;
      if (!best || bar.height < best.height) best = bar;
    }
    return best;
  }

  /**
   * What would light up at a point, from the page alone: for a slide its
   * block; for a paper the sentence drawn there, else its paragraph.
   */
  _preview(entry, event) {
    const point = this.viewer.clientPointToPdf(entry, event.clientX, event.clientY);
    const slide = this.slides.sourceAt(entry.num, point);
    if (slide) {
      return {
        key: `${slide.file}:${slide.line}-${slide.endLine}`,
        rects: slide.rects,
        whole: !slide.rects.length,
      };
    }
    const bar = this._paragraphAt(entry.num, point);
    const sentence = sentenceAt(this.viewer, entry, event.clientX, event.clientY, bar);
    if (sentence) {
      const first = sentence.rects[0];
      return {
        key: `s:${entry.num}:${Math.round(first.x)}:${Math.round(first.y)}:${sentence.text.length}`,
        rects: sentence.rects.map((r) => ({ page: entry.num, ...r })),
        sentence,
      };
    }
    if (bar) return { key: `p:${bar.file}:${bar.index}:${bar.page}`, rects: [bar] };
    return null;
  }

  /* ---------------- lighting up ---------------- */

  _moved(event) {
    clearTimeout(this._timer);
    if (event.target.closest?.(NOT_PAGE)) {
      this._unhover();
      return;
    }
    const { clientX, clientY } = event;
    this._timer = setTimeout(() => {
      const entry = this.viewer.pageAtClientPoint(clientX, clientY);
      const found = entry?.viewport ? this._preview(entry, { clientX, clientY }) : null;
      if (!found) this._unhover();
      else this._light(entry, found);
    }, HOVER_MS);
  }

  _layerFor(entry) {
    let layer = entry.el.querySelector('.pick-layer');
    if (!layer) {
      layer = document.createElement('div');
      layer.className = 'pick-layer';
      entry.el.append(layer);
    }
    return layer;
  }

  _light(entry, { key, rects, whole = false }) {
    // Unchanged, and still drawn: a page render wipes what is overlaid on it.
    if (this.hover?.key === key && this.hover.nodes.every((node) => node.isConnected)) return;
    this._unhover();
    const nodes = [];
    for (const rect of rects) {
      const on = this.viewer.pageEntry(rect.page ?? entry.num);
      if (!on?.viewport) continue;
      const r = this.viewer.pdfRectToPageRect(on, rect);
      const node = document.createElement('div');
      node.className = 'pick-hover';
      Object.assign(node.style, {
        left: `${r.left - 3}px`,
        top: `${r.top - 2}px`,
        width: `${r.width + 6}px`,
        height: `${r.height + 4}px`,
      });
      this._layerFor(on).append(node);
      nodes.push(node);
    }
    // No lines of its own: the page itself is what a click would pick.
    if (whole) entry.el.classList.add('pick-whole');
    this.hover = { key, nodes, el: whole ? entry.el : null };
  }

  _unhover() {
    clearTimeout(this._timer);
    if (!this.hover) return;
    for (const node of this.hover.nodes) node.remove();
    this.hover.el?.classList.remove('pick-whole');
    this.hover = null;
  }

  /* ---------------- picking ---------------- */

  async _pick(entry, event) {
    const point = this.viewer.clientPointToPdf(entry, event.clientX, event.clientY);
    const anchor = { ...point, width: 0, height: 0 };
    const preview = this._preview(entry, event);

    // The same piece again: widen it.
    if (preview && this.last?.key === preview.key && this.last.wider) {
      const wider = this.last.wider();
      this.last = { key: preview.key, wider: wider.wider || null, widened: true };
      this._unhover();
      this.onPick?.(wider, event);
      return;
    }

    const slide = this.slides.sourceAt(entry.num, point);
    if (slide) {
      const passage = passageFor(entry, slide, anchor);
      if (slide.kind !== 'slide') {
        const frame = this.slides.frameSource(entry.num);
        if (frame) passage.wider = () => passageFor(entry, frame, anchor);
      }
      this._picked(preview, passage, event);
      return;
    }

    if (preview) this._light(entry, preview); // a tap has no hover to have lit it
    const asked = ++this._asking;
    let passage;
    try {
      passage = await this._paperPassage(entry, event, anchor, preview);
    } catch (err) {
      if (asked === this._asking) this.onError?.(err.message || String(err));
      return;
    }
    if (asked !== this._asking) return; // a later pick has taken over
    this._picked(preview, passage, event);
  }

  _picked(preview, passage, event) {
    this.last = preview ? { key: preview.key, wider: passage.wider || null } : null;
    this._unhover();
    this.onPick?.(passage, event);
  }

  /** A pick on a paper: its sentence, which widens to its paragraph. */
  async _paperPassage(entry, event, anchor, preview) {
    const word = this.viewer.wordAtClientPoint(event.clientX, event.clientY);
    const where = await postJSON('/api/resolve', {
      page: entry.num,
      x: Number(anchor.x.toFixed(3)),
      y: Number(anchor.y.toFixed(3)),
      selectedText: null,
      word: word?.word ?? null,
      contextBefore: word?.before ?? null,
      contextAfter: word?.after ?? null,
    });
    const block = await getJSON(
      `/api/block?file=${encodeURIComponent(where.file)}&line=${encodeURIComponent(where.line)}`
    );

    const paragraph = {
      file: block.file,
      line: block.paragraph.start,
      endLine: block.paragraph.end,
      at: where.line,
      column: 1,
      word: null,
      kind: 'paragraph',
      text: block.paragraph.text,
      rects: block.paragraph.rects,
      page: entry.num,
    };
    const whole = () => passageFor(entry, paragraph, anchor);

    const drawn = preview?.sentence || null;
    const oneSpan =
      block.sentence.start === block.paragraph.start && block.sentence.end === block.paragraph.end;
    // No sentence to be had: math, a caption, a heading. The paragraph it is.
    if (!drawn && oneSpan) return whole();

    const sentence = {
      file: block.file,
      line: block.sentence.start,
      endLine: block.sentence.end,
      at: where.line,
      column: 1,
      word: null,
      kind: 'sentence',
      // As the page words it when it can be read there: that is what tells
      // one sentence from the next where they share a source line.
      text: drawn?.text || block.sentence.text,
      rects: drawn ? drawn.rects.map((r) => ({ page: entry.num, ...r })) : block.sentence.rects,
      page: entry.num,
    };
    const passage = passageFor(entry, sentence, anchor);
    passage.wider = whole;
    return passage;
  }
}
