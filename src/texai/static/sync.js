// Keeping the source and the page on the same passage.
//
// The two panes show the same document, and by default they only meet when you
// ask: click a line to see it on the page, Cmd-click a passage to see its
// source. Reading with both open means doing that over and over. Switched on,
// this follows whichever pane you are scrolling and moves the other to match —
// SyncTeX both ways, the same mapping the clicks use.
//
// Two rules keep it from fighting itself. A pane that has just been moved for
// you ignores its own scroll events for a moment, so an answer never bounces
// back as a new question; and a lookup is only worth making when the top of
// the view has actually moved somewhere new.

import { getJSON, postJSON } from './api.js';

const SETTLE_MS = 180; // quiet time after the last scroll event before looking anything up
const QUIET_MS = 700; // how long a pane disregards scrolling it did not cause
const SCAN_LINES = 25; // how far past an unmappable line to look for one that rendered
const TOP_ALIGN = 0.18; // where in the view the matched spot lands, as a fraction

// Where on the page to ask "what is here?". The very top edge is often a
// margin, a running head, or the gap above a float, none of which SyncTeX has
// anything to say about; a little way in is text far more often.
const PROBES = [0.14, 0.32, 0.55];

export class ScrollSync {
  constructor({ viewer, editor, button, storageKey = 'texai.sync' }) {
    this.viewer = viewer;
    this.editor = editor;
    this.button = button;
    this.storageKey = storageKey;

    this.on = false;
    this.busy = false;
    this._timer = null;
    this._driver = null;
    // Until when each pane should disregard its own scrolling.
    this._quiet = { pdf: 0, source: 0 };
    // The last place each direction asked about, so a scroll that lands on the
    // same line — or the same rendered spot — costs nothing.
    this._asked = { source: null, pdf: null };

    this._wire();
  }

  _wire() {
    this.button?.addEventListener('click', () => this.toggle());

    this.viewer.container.addEventListener('scroll', () => this._moved('pdf'), {
      passive: true,
    });
    this.editor.onViewMoved = () => this._moved('source');

    let stored = null;
    try {
      stored = localStorage.getItem(this.storageKey);
    } catch {
      /* storage disabled: the mode is then simply off at every start */
    }
    if (stored === 'on') this.toggle(true);
    else this._paint();
  }

  toggle(force = null) {
    this.on = force === null ? !this.on : force;
    this._paint();
    try {
      localStorage.setItem(this.storageKey, this.on ? 'on' : 'off');
    } catch {
      /* the mode still works for this session */
    }
    clearTimeout(this._timer);
    this._asked = { source: null, pdf: null };
    if (this.on) this.align();
    else this.editor.clearSynced();
  }

  _paint() {
    if (!this.button) return;
    this.button.classList.toggle('active', this.on);
    this.button.setAttribute('aria-pressed', String(this.on));
  }

  /**
   * Bring the source to where the page is, now.
   *
   * The page leads when the mode is switched on and when the Source tab comes
   * back: it is the thing being read, and the editor is what should catch up.
   */
  align() {
    if (!this.on) return;
    clearTimeout(this._timer);
    this._asked.pdf = null;
    this._driver = 'pdf';
    this._timer = setTimeout(() => this._run('pdf'), 0);
  }

  /* ---------------- who is driving ---------------- */

  _moved(pane) {
    if (!this.on) return;
    // Scrolling this pane did not do: it was moved to keep up with the other.
    if (performance.now() < this._quiet[pane]) return;
    this._driver = pane;
    clearTimeout(this._timer);
    this._timer = setTimeout(() => this._run(pane), SETTLE_MS);
  }

  _hush(pane) {
    this._quiet[pane] = performance.now() + QUIET_MS;
  }

  async _run(pane) {
    if (!this.on) return;
    if (this.busy) {
      // A lookup is already out. Come back for this one rather than dropping
      // it, or a fast scroll would leave the panes on different passages.
      clearTimeout(this._timer);
      this._timer = setTimeout(() => this._run(pane), SETTLE_MS);
      return;
    }
    this.busy = true;
    try {
      if (pane === 'source') await this._pageFollows();
      else await this._sourceFollows();
    } catch {
      // A line SyncTeX cannot place, a point in a margin, a PDF mid-rebuild:
      // all of them just mean "stay where you are" until the next scroll.
    } finally {
      this.busy = false;
    }
  }

  /* ---------------- the source leads ---------------- */

  async _pageFollows() {
    const file = this.editor.file;
    const line = this.editor.topVisibleLine();
    if (!file || line == null) return;

    const key = `${file}:${line}`;
    if (key === this._asked.source) return;
    this._asked.source = key;

    const found = await getJSON(
      `/api/locate?file=${encodeURIComponent(file)}&line=${line}&scan=${SCAN_LINES}`
    );
    // Still ours to move? The mode may have been switched off, or the other
    // pane may have taken over, while the lookup was in flight.
    if (!this.on || !found?.found || this._driver !== 'source') return;

    this._hush('pdf');
    this.viewer.scrollToBox(found.page, found.boxes[0], { align: TOP_ALIGN });
  }

  /* ---------------- the page leads ---------------- */

  async _sourceFollows() {
    // Nothing to keep up to date while the editor is not the visible tab, and
    // CodeMirror cannot measure a hidden pane to scroll it anyway.
    if (!this.editor.active) return;

    const point = this._probe();
    if (!point) return;

    const key = `${point.page}:${Math.round(point.y)}`;
    if (key === this._asked.pdf) return;
    this._asked.pdf = key;

    const where = await postJSON('/api/resolve', point);
    if (!this.on || !where?.file || this._driver === 'source') return;

    this._hush('source');
    await this.editor.showLine(where.file, where.line);
  }

  /** A point in PDF coordinates near the top of what is on screen. */
  _probe() {
    const bounds = this.viewer.container.getBoundingClientRect();
    const x = bounds.left + bounds.width / 2;
    for (const fraction of PROBES) {
      const y = bounds.top + bounds.height * fraction;
      const entry = this.viewer.pageAtClientPoint(x, y);
      if (!entry?.viewport) continue;
      const pdf = this.viewer.clientPointToPdf(entry, x, y);
      return { page: entry.num, x: pdf.x, y: pdf.y };
    }
    return null;
  }
}
