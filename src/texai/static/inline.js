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

const FALLBACK_FONT_PT = 10;
const MIN_FONT_PT = 6;
const MAX_FONT_PT = 24;
const PAD_PX = 6; // the box reaches this far past the text column, so the text inside lines up with it
// A saved box waits this long for the rebuilt PDF to arrive before closing anyway.
const SETTLE_MS = 6000;

const IS_MAC = navigator.platform.toUpperCase().includes('MAC');

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
    this.session = {
      file: para.file,
      start: para.start,
      end: para.end,
      original: para.text,
      page: entry.num,
      box: this._boxFor(entry, para, click, fontPt),
      fontPt,
      state: 'editing',
      reloaded: false,
      settle: null,
      ...this._build(para),
    };
    this.place();
    this._putCursor(where);
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
    const pageRect = entry.el.getBoundingClientRect();
    const hostRect = this.container.getBoundingClientRect();
    const left = pageRect.left - hostRect.left + this.container.scrollLeft + rect.left;
    const top = pageRect.top - hostRect.top + this.container.scrollTop + rect.top;

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

  _build(para) {
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
    const cancel = document.createElement('button');
    cancel.type = 'button';
    cancel.textContent = 'Cancel';
    cancel.addEventListener('click', () => this.cancel());
    const save = document.createElement('button');
    save.type = 'button';
    save.className = 'primary';
    save.textContent = 'Save';
    save.addEventListener('click', () => this.commit());
    bar.append(status, cancel, save);

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
    return { el, cm, status, save };
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
    const ch = where.word ? Math.max(0, Math.min(text.length, where.column - 1)) : 0;
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
    const text = s.cm.getValue();
    if (text === s.original) {
      this.close();
      return;
    }

    this._setState('saving');
    this._setStatus('Saving and rebuilding…', 'busy');
    let result;
    try {
      result = await postJSON('/api/paragraph', {
        file: s.file,
        start: s.start,
        end: s.end,
        original: s.original,
        text,
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
    s.original = result.text;

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
