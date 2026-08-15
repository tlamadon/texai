// Go to anything: a section in this file, a section in another one, or a file.
//
// The list is built from the same outline the column beside the editor shows.
// For the file being edited it comes from the buffer, so it is right even with
// unsaved edits; for the rest of the project the files are read once and cached
// until something writes to them — you or the agent.

import { getJSON } from './api.js';
import { parseOutline } from './outline.js';

const MAX_RESULTS = 60;
const MAX_SCANNED_FILES = 80;

/**
 * Score `text` against a typed query, subsequence-style.
 *
 * Returns null when the query does not fit at all. Matches that land on word
 * boundaries and runs of consecutive characters score highest, so "modh" finds
 * "Model / Households" ahead of anything that merely contains those letters.
 */
export function fuzzy(query, text) {
  if (!query) return { score: 0, positions: [] };
  const needle = query.toLowerCase();
  const hay = text.toLowerCase();
  const positions = [];
  let score = 0;
  let from = 0;
  let run = 0;

  for (let i = 0; i < needle.length; i += 1) {
    const ch = needle[i];
    if (ch === ' ') {
      run = 0; // a space is a word break in the query, not something to match
      continue;
    }
    const at = hay.indexOf(ch, from);
    if (at === -1) return null;
    score += 10;
    if (at === from && positions.length) {
      run += 1;
      score += 8 + run * 2;
    } else {
      run = 0;
    }
    if (at === 0 || /[\s/_\-.:(]/.test(hay[at - 1])) score += 12;
    if (text[at] === query[i]) score += 2;
    positions.push(at);
    from = at + 1;
  }
  // Among equally good matches, prefer the shorter, tighter label.
  return { score: score - Math.max(0, text.length - needle.length) * 0.15, positions };
}

export class QuickJump {
  /**
   * @param {object} sources
   *  - files(): Promise<string[]> — every editable file in the project
   *  - current(): {file, entries} — the buffer's own outline, live
   *  - onPick(file, line): jump there (line null just opens the file)
   */
  constructor(sources) {
    this.sources = sources;
    this.els = {
      root: document.getElementById('jump'),
      input: document.getElementById('jump-input'),
      list: document.getElementById('jump-list'),
      count: document.getElementById('jump-count'),
    };
    this.open = false;
    this.items = [];
    this.index = 0;
    this.outlines = new Map(); // file -> entries, for files not being edited
    this.scanned = false;

    this._wire();
  }

  _wire() {
    this.els.input.addEventListener('input', () => this._render());
    this.els.input.addEventListener('keydown', (event) => this._onKey(event));
    // A click on the backdrop, but not inside the box, dismisses it.
    this.els.root.addEventListener('mousedown', (event) => {
      if (event.target === this.els.root) this.close();
    });
  }

  toggle() {
    if (this.open) this.close();
    else this.show();
  }

  show() {
    this.open = true;
    this.els.root.hidden = false;
    this.els.input.value = '';
    this.index = 0;
    this._render();
    this.els.input.focus();
    // The project scan is a fetch per file, so it waits until someone actually
    // asks to jump; results fold in when it lands.
    this._scanProject().then(() => this.open && this._render());
  }

  /** @param {boolean} refocus - hand the keyboard back; a jump does that itself */
  close(refocus = true) {
    this.open = false;
    this.els.root.hidden = true;
    if (refocus) this.sources.onDismiss?.();
  }

  /** Something wrote to the project — the cached outlines may be stale. */
  invalidate() {
    this.outlines.clear();
    this.scanned = false;
  }

  async _scanProject() {
    if (this.scanned) return;
    this.scanned = true;
    let files = [];
    try {
      files = await this.sources.files();
    } catch {
      this.scanned = false; // the buffer's own outline still works; try again later
      return;
    }
    const tex = files.filter((file) => file.endsWith('.tex')).slice(0, MAX_SCANNED_FILES);
    await Promise.all(
      tex.map(async (file) => {
        if (this.outlines.has(file)) return;
        try {
          const data = await getJSON(`/api/source?file=${encodeURIComponent(file)}`);
          this.outlines.set(file, parseOutline(data.text));
        } catch {
          this.outlines.set(file, []);
        }
      })
    );
    this.files = files;
  }

  /** Everything that can be jumped to, as `{label, detail, file, line, boost}`. */
  _candidates() {
    const { file: openFile, entries } = this.sources.current();
    const items = [];

    for (const entry of entries || []) {
      if (entry.kind === 'input') continue;
      items.push({
        label: entry.title,
        detail: `${openFile}:${entry.line}`,
        file: openFile,
        line: entry.line,
        kind: entry.kind,
        boost: 30, // where you already are is usually where you meant
      });
    }
    for (const [file, list] of this.outlines) {
      if (file === openFile) continue;
      for (const entry of list) {
        if (entry.kind === 'input') continue;
        items.push({
          label: entry.title,
          detail: `${file}:${entry.line}`,
          file,
          line: entry.line,
          kind: entry.kind,
          boost: 0,
        });
      }
    }
    for (const file of this.files || []) {
      items.push({ label: file, detail: 'file', file, line: null, kind: 'file', boost: -10 });
    }
    return items;
  }

  _render() {
    const query = this.els.input.value.trim();
    const scored = [];
    for (const item of this._candidates()) {
      const hit = fuzzy(query, item.label);
      if (!hit) continue;
      scored.push({ ...item, score: hit.score + item.boost, positions: hit.positions });
    }
    // Without a query the source order is the document's own, which reads
    // better than any score would.
    if (query) scored.sort((a, b) => b.score - a.score);
    this.items = scored.slice(0, MAX_RESULTS);
    this.index = Math.min(this.index, Math.max(0, this.items.length - 1));

    if (!this.items.length) {
      const empty = document.createElement('li');
      empty.className = 'jump-empty';
      empty.textContent = query ? `Nothing matches “${query}”.` : 'Nothing to jump to yet.';
      this.els.list.replaceChildren(empty);
      this.els.count.textContent = '';
      return;
    }

    this.els.list.replaceChildren(
      ...this.items.map((item, i) => {
        const row = document.createElement('li');
        row.className = `jump-row kind-${item.kind}${i === this.index ? ' active' : ''}`;
        row.setAttribute('role', 'option');

        const label = document.createElement('span');
        label.className = 'jump-label';
        label.append(...highlight(item.label, item.positions));

        const detail = document.createElement('span');
        detail.className = 'jump-detail';
        detail.textContent = item.detail;

        row.append(label, detail);
        row.addEventListener('mousedown', (event) => {
          event.preventDefault(); // keep focus in the input until we jump
          this._pick(i);
        });
        return row;
      })
    );
    this.els.count.textContent = `${this.items.length}${this.items.length === MAX_RESULTS ? '+' : ''}`;
    this._scrollToActive();
  }

  _scrollToActive() {
    this.els.list.children[this.index]?.scrollIntoView({ block: 'nearest' });
  }

  _move(delta) {
    if (!this.items.length) return;
    const previous = this.els.list.children[this.index];
    previous?.classList.remove('active');
    this.index = (this.index + delta + this.items.length) % this.items.length;
    this.els.list.children[this.index]?.classList.add('active');
    this._scrollToActive();
  }

  _pick(index = this.index) {
    const item = this.items[index];
    if (!item) return;
    // Close first: the jump puts the cursor in the editor, and the palette
    // would otherwise take the focus straight back.
    this.close(false);
    this.sources.onPick(item.file, item.line);
  }

  _onKey(event) {
    if (event.key === 'ArrowDown' || (event.key === 'n' && event.ctrlKey)) {
      event.preventDefault();
      this._move(1);
    } else if (event.key === 'ArrowUp' || (event.key === 'p' && event.ctrlKey)) {
      event.preventDefault();
      this._move(-1);
    } else if (event.key === 'Enter') {
      event.preventDefault();
      this._pick();
    } else if (event.key === 'Escape') {
      event.preventDefault();
      this.close();
    }
  }
}

/** Split a label into plain and matched runs, for the <b> marks in a result. */
function highlight(text, positions) {
  const marked = new Set(positions);
  const nodes = [];
  let buffer = '';
  let bufferMarked = false;

  const flush = () => {
    if (!buffer) return;
    if (bufferMarked) {
      const strong = document.createElement('b');
      strong.textContent = buffer;
      nodes.push(strong);
    } else {
      nodes.push(document.createTextNode(buffer));
    }
    buffer = '';
  };

  for (let i = 0; i < text.length; i += 1) {
    const isMarked = marked.has(i);
    if (isMarked !== bufferMarked) {
      flush();
      bufferMarked = isMarked;
    }
    buffer += text[i];
  }
  flush();
  return nodes;
}
