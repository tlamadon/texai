// Minimal continuous PDF.js viewer: canvas + text layer per page, lazy
// rendering, zoom, and coordinate conversion from screen pixels to the
// top-left-origin PDF points that SyncTeX expects.

import * as pdfjsLib from './vendor/pdfjs/pdf.mjs';

pdfjsLib.GlobalWorkerOptions.workerSrc = new URL(
  './vendor/pdfjs/pdf.worker.mjs',
  import.meta.url
).href;

const MAX_DPR = 2;
const RENDER_MARGIN_PX = 600; // render this far outside the viewport
const DISCARD_MARGIN_PX = 3000; // free canvases beyond this
const MIN_SCALE = 0.25;
const MAX_SCALE = 5;
const ZOOM_STEPS = [0.5, 0.67, 0.8, 1, 1.25, 1.5, 1.75, 2, 2.5, 3, 4];

const clamp = (value, lo, hi) => Math.min(hi, Math.max(lo, value));

// Letters and digits, with internal hyphens and apostrophes: "well-being",
// "don't", "R^2" splits, which is what we want for matching.
const WORD_RE = /[\p{L}\p{N}][\p{L}\p{N}'\u2019-]*/gu;

export class PdfViewer {
  constructor({ container, viewerEl, onPageChange = () => {}, onRender = () => {} }) {
    this.container = container;
    this.viewerEl = viewerEl;
    this.onPageChange = onPageChange;
    this.onRender = onRender;

    this.doc = null;
    this.pages = [];
    this.scale = 1.25;
    this.fitWidth = true;
    this.currentPage = 1;

    this._scheduled = false;
    const schedule = () => {
      if (this._scheduled) return;
      this._scheduled = true;
      requestAnimationFrame(() => {
        this._scheduled = false;
        this._updateCurrentPage();
        this._renderVisible();
      });
    };
    this.container.addEventListener('scroll', schedule, { passive: true });
    window.addEventListener('resize', () => {
      if (this.fitWidth) this.setFitWidth();
      else schedule();
    });
  }

  get pageCount() {
    return this.doc ? this.doc.numPages : 0;
  }

  /** Load (or reload) a PDF from `url`, replacing whatever is displayed. */
  async load(url) {
    const doc = await pdfjsLib.getDocument({ url, isEvalSupported: false }).promise;
    const previous = this.doc;
    // Stop work on the outgoing document before dropping its elements.
    for (const entry of this.pages) this._resetPage(entry);
    this.doc = doc;
    this.pages = [];
    this.viewerEl.replaceChildren();

    for (let num = 1; num <= doc.numPages; num += 1) {
      const page = await doc.getPage(num);
      const el = document.createElement('div');
      el.className = 'page';
      el.dataset.pageNumber = String(num);
      this.viewerEl.append(el);
      this.pages.push({ num, page, el, viewport: null, rendered: false, task: null, textLayer: null });
    }

    if (previous) previous.destroy().catch(() => {});
    if (this.fitWidth) this._computeFitScale();
    this._applyScale();
  }

  /* ---------------- zoom ---------------- */

  setScale(scale, { fit = false } = {}) {
    const next = clamp(scale, MIN_SCALE, MAX_SCALE);
    if (next === this.scale && fit === this.fitWidth) return;
    const state = this.getViewState();
    this.scale = next;
    this.fitWidth = fit;
    this._applyScale();
    this.scrollToPage(state.page, state.offsetRatio);
  }

  zoomIn() {
    const next = ZOOM_STEPS.find((s) => s > this.scale + 1e-6);
    this.setScale(next ?? this.scale * 1.25);
  }

  zoomOut() {
    const next = [...ZOOM_STEPS].reverse().find((s) => s < this.scale - 1e-6);
    this.setScale(next ?? this.scale / 1.25);
  }

  setFitWidth() {
    const state = this.getViewState();
    this.fitWidth = true;
    this._computeFitScale();
    this._applyScale();
    this.scrollToPage(state.page, state.offsetRatio);
  }

  _computeFitScale() {
    const first = this.pages[0];
    if (!first) return;
    const unscaled = first.page.getViewport({ scale: 1 });
    const available = this.container.clientWidth - 48;
    this.scale = clamp(available / unscaled.width, MIN_SCALE, MAX_SCALE);
  }

  _applyScale() {
    for (const entry of this.pages) {
      entry.viewport = entry.page.getViewport({ scale: this.scale });
      this._resetPage(entry);
      const { style } = entry.el;
      style.width = `${Math.floor(entry.viewport.width)}px`;
      style.height = `${Math.floor(entry.viewport.height)}px`;
      // pdf.js sizes text-layer spans from these custom properties.
      style.setProperty('--scale-factor', String(this.scale));
      style.setProperty('--total-scale-factor', String(this.scale));
      style.setProperty('--scale-round-x', '1px');
      style.setProperty('--scale-round-y', '1px');
    }
    this._renderVisible();
    this._updateCurrentPage();
    this.onRender(null); // scale changed: anything overlaid needs repositioning
  }

  /* ---------------- rendering ---------------- */

  _resetPage(entry) {
    entry.task?.cancel();
    entry.task = null;
    entry.textLayer?.cancel?.();
    entry.textLayer = null;
    entry.rendered = false;
    entry.el.replaceChildren();
  }

  _renderVisible() {
    const bounds = this.container.getBoundingClientRect();
    for (const entry of this.pages) {
      const rect = entry.el.getBoundingClientRect();
      const distance = Math.max(bounds.top - rect.bottom, rect.top - bounds.bottom);
      if (distance < RENDER_MARGIN_PX) {
        this._renderPage(entry);
      } else if (entry.rendered && distance > DISCARD_MARGIN_PX) {
        this._resetPage(entry);
      }
    }
  }

  async _renderPage(entry) {
    if (entry.rendered || entry.task) return;
    const { viewport } = entry;
    const dpr = Math.min(window.devicePixelRatio || 1, MAX_DPR);

    const canvas = document.createElement('canvas');
    canvas.width = Math.floor(viewport.width * dpr);
    canvas.height = Math.floor(viewport.height * dpr);
    canvas.style.width = `${Math.floor(viewport.width)}px`;
    canvas.style.height = `${Math.floor(viewport.height)}px`;
    entry.el.append(canvas);

    const task = entry.page.render({
      canvasContext: canvas.getContext('2d', { alpha: false }),
      viewport,
      transform: dpr === 1 ? null : [dpr, 0, 0, dpr, 0, 0],
    });
    entry.task = task;
    try {
      await task.promise;
    } catch (err) {
      if (err?.name !== 'RenderingCancelledException') throw err;
      return;
    } finally {
      if (entry.task === task) entry.task = null;
    }
    entry.rendered = true;

    const textDiv = document.createElement('div');
    textDiv.className = 'textLayer';
    entry.el.append(textDiv);
    const textLayer = new pdfjsLib.TextLayer({
      textContentSource: entry.page.streamTextContent(),
      container: textDiv,
      viewport,
    });
    entry.textLayer = textLayer;

    // Twice, deliberately. Overlays that only need geometry appear at once;
    // anything that reads the rendered text — matching changed words against
    // the text layer — needs the spans, which do not exist until render()
    // resolves. Drawing is idempotent, so the second pass just refines.
    this.onRender(entry);
    textLayer
      .render()
      .then(() => this.onRender(entry))
      .catch(() => {});
  }

  /* ---------------- scroll position ---------------- */

  _updateCurrentPage() {
    const bounds = this.container.getBoundingClientRect();
    const middle = bounds.top + bounds.height / 2;

    // The page you are actually looking at is the one under the middle of the
    // viewport, not whichever page has a sliver at the top — otherwise jumping
    // to something on page 4 leaves the indicator reading 3.
    let current = null;
    for (const entry of this.pages) {
      const rect = entry.el.getBoundingClientRect();
      if (rect.top <= middle && rect.bottom >= middle) {
        current = entry.num;
        break;
      }
    }
    if (current === null) {
      // The centre can fall in the gap between two pages.
      for (const entry of this.pages) {
        const rect = entry.el.getBoundingClientRect();
        if (rect.bottom > bounds.top + 1) {
          current = entry.num;
          break;
        }
      }
    }

    if (current !== null && current !== this.currentPage) {
      this.currentPage = current;
      this.onPageChange(current, this.pageCount);
    }
  }

  /** Page + fractional offset within it, enough to restore the view after a reload. */
  getViewState() {
    const bounds = this.container.getBoundingClientRect();
    for (const entry of this.pages) {
      const rect = entry.el.getBoundingClientRect();
      if (rect.bottom > bounds.top + 1) {
        return {
          scale: this.scale,
          fitWidth: this.fitWidth,
          page: entry.num,
          offsetRatio: rect.height ? (bounds.top - rect.top) / rect.height : 0,
          scrollLeft: this.container.scrollLeft,
        };
      }
    }
    return { scale: this.scale, fitWidth: this.fitWidth, page: 1, offsetRatio: 0, scrollLeft: 0 };
  }

  applyViewState(state) {
    if (!state) return;
    this.fitWidth = state.fitWidth;
    if (state.fitWidth) this._computeFitScale();
    else this.scale = clamp(state.scale, MIN_SCALE, MAX_SCALE);
    this._applyScale();
    this.scrollToPage(state.page, state.offsetRatio);
    this.container.scrollLeft = state.scrollLeft ?? 0;
  }

  scrollToPage(pageNumber, offsetRatio = 0) {
    const entry = this.pages[clamp(pageNumber, 1, this.pages.length) - 1];
    if (!entry) return;
    const rect = entry.el.getBoundingClientRect();
    const bounds = this.container.getBoundingClientRect();
    this.container.scrollTop += rect.top - bounds.top + offsetRatio * rect.height;
    this._updateCurrentPage();
    this._renderVisible();
  }

  /* ---------------- coordinates ---------------- */

  pageAtClientPoint(clientX, clientY) {
    for (const entry of this.pages) {
      const rect = entry.el.getBoundingClientRect();
      if (
        clientX >= rect.left &&
        clientX <= rect.right &&
        clientY >= rect.top &&
        clientY <= rect.bottom
      ) {
        return entry;
      }
    }
    return null;
  }

  /**
   * Convert a viewport point to PDF points measured from the page's top-left
   * corner — the convention `synctex edit` uses.
   */
  clientPointToPdf(entry, clientX, clientY) {
    const rect = entry.el.getBoundingClientRect();
    const [x, y] = entry.viewport.convertToPdfPoint(clientX - rect.left, clientY - rect.top);
    const [bx0, by0, bx1, by1] = entry.viewport.viewBox;
    return {
      x: clamp(x - bx0, 0, bx1 - bx0),
      y: clamp(by1 - y, 0, by1 - by0),
    };
  }

  /** Look up a rendered page by its 1-based number. */
  pageEntry(pageNumber) {
    return this.pages[pageNumber - 1] || null;
  }

  /**
   * Convert a PDF-point rectangle (top-left origin, as SyncTeX reports) into
   * CSS pixels inside the page element. The exact inverse of
   * `clientPointToPdf`, so a marker lands on the text a click would have hit.
   */
  pdfRectToPageRect(entry, rect) {
    const [bx0, , , by1] = entry.viewport.viewBox;
    const [x1, y1] = entry.viewport.convertToViewportPoint(rect.x + bx0, by1 - rect.y);
    const [x2, y2] = entry.viewport.convertToViewportPoint(
      rect.x + rect.width + bx0,
      by1 - (rect.y + rect.height)
    );
    return {
      left: Math.min(x1, x2),
      top: Math.min(y1, y2),
      width: Math.abs(x2 - x1),
      height: Math.abs(y2 - y1),
    };
  }

  /**
   * Scroll a PDF-point box into the middle of the view.
   *
   * Page elements are always laid out (they carry their size before they
   * render), so this works even for a page whose canvas has not been drawn yet
   * — rendering follows the scroll.
   */
  scrollToBox(pageNumber, box) {
    const entry = this.pageEntry(pageNumber);
    if (!entry || !entry.viewport) return false;

    const rect = box ? this.pdfRectToPageRect(entry, box) : { top: 0, height: 0 };
    const pageRect = entry.el.getBoundingClientRect();
    const bounds = this.container.getBoundingClientRect();
    const delta =
      pageRect.top - bounds.top + rect.top + rect.height / 2 - bounds.height / 2;

    this.container.scrollTop += delta;
    this._updateCurrentPage();
    this._renderVisible();
    return true;
  }

  /** Briefly highlight a box, so the eye lands where the view just jumped. */
  flashBox(pageNumber, box) {
    const entry = this.pageEntry(pageNumber);
    if (!entry || !entry.viewport) return;

    const rect = this.pdfRectToPageRect(entry, box);
    const node = document.createElement('div');
    node.className = 'flash';
    node.style.left = `${rect.left}px`;
    node.style.top = `${rect.top}px`;
    node.style.width = `${Math.max(rect.width, 8)}px`;
    node.style.height = `${Math.max(rect.height, 6)}px`;
    entry.el.append(node);
    setTimeout(() => node.remove(), 2400);
  }

  /**
   * The word under a point, with the rendered words either side of it.
   *
   * SyncTeX only ever answers with a line, so this is where the extra precision
   * comes from: the text layer knows which word was clicked, and the server
   * matches it back into the source. The neighbours are what tell one "the"
   * from the next.
   */
  wordAtClientPoint(clientX, clientY) {
    const caret = this._caretAt(clientX, clientY);
    if (!caret) return null;
    const { node, offset } = caret;
    if (node.nodeType !== Node.TEXT_NODE) return null;
    const span = node.parentElement;
    if (!span || !span.closest('.textLayer')) return null;

    const text = node.textContent || '';
    const words = [...text.matchAll(WORD_RE)];
    // The caret can land in the space between words; the one just before it is
    // what the user was pointing at.
    let index = words.findIndex((m) => offset >= m.index && offset <= m.index + m[0].length);
    if (index === -1) index = words.findIndex((m) => m.index > offset) - 1;
    if (index < 0) index = words.length - 1;
    const match = words[index];
    if (!match) return null;

    const line = this._renderedLine(span);
    const inLine = line ? [...line.matchAll(WORD_RE)].map((m) => m[0]) : [];
    // Locate the word within the whole rendered line so the context spans
    // whatever the text layer happened to split into separate elements.
    const before = words.slice(Math.max(0, index - 3), index).map((m) => m[0]);
    const after = words.slice(index + 1, index + 4).map((m) => m[0]);

    if ((!before.length || !after.length) && inLine.length) {
      const at = this._indexInLine(inLine, match[0], before, after);
      if (at !== -1) {
        return {
          word: match[0],
          before: inLine.slice(Math.max(0, at - 3), at),
          after: inLine.slice(at + 1, at + 4),
        };
      }
    }
    return { word: match[0], before, after };
  }

  /** Where a word sits among the words of its rendered line. */
  _indexInLine(words, word, before, after) {
    const spots = [];
    words.forEach((w, i) => w === word && spots.push(i));
    if (spots.length <= 1) return spots[0] ?? -1;
    // Several of the same word on the line: pick the one whose neighbours agree.
    let best = spots[0];
    let bestScore = -1;
    for (const spot of spots) {
      let score = 0;
      before.forEach((w, i) => words[spot - before.length + i] === w && (score += 1));
      after.forEach((w, i) => words[spot + 1 + i] === w && (score += 1));
      if (score > bestScore) {
        bestScore = score;
        best = spot;
      }
    }
    return best;
  }

  /** The text of every span sharing a visual line with this one, left to right. */
  _renderedLine(span) {
    const layer = span.closest('.textLayer');
    if (!layer) return '';
    const rect = span.getBoundingClientRect();
    if (!rect.height) return '';
    const middle = rect.top + rect.height / 2;

    const row = [...layer.querySelectorAll('span')]
      .map((el) => ({ el, r: el.getBoundingClientRect() }))
      .filter(({ r }) => r.height > 0 && Math.abs(r.top + r.height / 2 - middle) < rect.height * 0.6)
      .sort((a, b) => a.r.left - b.r.left);

    return row.map(({ el }) => el.textContent).join(' ');
  }

  /** One caret API or the other, depending on the browser. */
  _caretAt(clientX, clientY) {
    if (document.caretRangeFromPoint) {
      const range = document.caretRangeFromPoint(clientX, clientY);
      return range ? { node: range.startContainer, offset: range.startOffset } : null;
    }
    if (document.caretPositionFromPoint) {
      const position = document.caretPositionFromPoint(clientX, clientY);
      return position ? { node: position.offsetNode, offset: position.offset } : null;
    }
    return null;
  }

  /**
   * The current text selection inside the viewer, as text plus the midpoint of
   * its bounding box (a stable point to hand to SyncTeX).
   */
  getSelectionInfo() {
    const selection = window.getSelection();
    if (!selection || selection.isCollapsed || selection.rangeCount === 0) return null;
    const range = selection.getRangeAt(0);
    if (!this.viewerEl.contains(range.commonAncestorContainer)) return null;

    const text = selection.toString().trim();
    if (!text) return null;

    const rects = Array.from(range.getClientRects()).filter((r) => r.width > 0 && r.height > 0);
    if (!rects.length) return null;

    const union = rects.reduce(
      (acc, r) => ({
        left: Math.min(acc.left, r.left),
        top: Math.min(acc.top, r.top),
        right: Math.max(acc.right, r.right),
        bottom: Math.max(acc.bottom, r.bottom),
      }),
      { left: Infinity, top: Infinity, right: -Infinity, bottom: -Infinity }
    );

    const candidates = [
      { x: (union.left + union.right) / 2, y: (union.top + union.bottom) / 2 },
      { x: (rects[0].left + rects[0].right) / 2, y: (rects[0].top + rects[0].bottom) / 2 },
    ];
    for (const point of candidates) {
      const entry = this.pageAtClientPoint(point.x, point.y);
      if (entry) return { text, entry, clientX: point.x, clientY: point.y };
    }
    return null;
  }
}
