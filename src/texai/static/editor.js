// A small LaTeX editor over the same project the agent edits.
//
// Two writers on one tree, so every save carries the hash of the text it was
// based on: if the agent rewrote the file underneath, the save is refused
// rather than silently discarding its work. Saving rebuilds the PDF, and the
// watcher in app.js reloads the page as it would after any other build.
//
// Refusing is the last resort, not the first. The agent's edit is usually in
// another part of the file entirely, so when it lands the buffer keeps the
// text it was loaded from and asks the server to merge the three versions.
// A merge that touches none of the unsaved lines is applied here and now, the
// lines it brought in are coloured, and the bar says how many arrived.

import { getJSON, postJSON } from './api.js';
import { phraseAt } from './marks.js';
import { OutlinePanel, parseOutline } from './outline.js';
import { resolveInput } from './project.js';
import { showToast } from './toast.js';

export class SourceEditor {
  constructor({ onSaved } = {}) {
    this.els = {
      pane: document.getElementById('editor-pane'),
      host: document.getElementById('editor-host'),
      picker: document.getElementById('editor-file'),
      save: document.getElementById('editor-save'),
      revert: document.getElementById('editor-revert'),
      menu: document.getElementById('editor-menu'),
      menuPop: document.getElementById('editor-menu-pop'),
      status: document.getElementById('editor-status'),
      dirty: document.getElementById('editor-dirty'),
      merged: document.getElementById('editor-merged'),
      search: document.getElementById('outline-search'),
      tab: document.getElementById('tab-source'),
    };

    this.cm = null;
    this.file = null;
    this.sha = null;
    // The text this buffer was loaded from — one of the three sides of a
    // merge, and the only one nobody else can reconstruct once it is gone.
    this.baseText = '';
    this.dirty = false;
    this.saving = false;
    this.syncing = false;
    // Line handles for what the last merge brought in, so the highlight
    // follows the lines as they move rather than the numbers they had.
    this._mergedLines = [];
    this._mergedSpots = [];
    this._mergedAt = 0;
    this.active = false;
    this.filesLoaded = false;
    this.entries = [];
    this.onSaved = onSaved || null;
    this.onActivate = null; // app.js hides the composer while editing
    this.onReveal = null;   // show this source line in the PDF
    this.onJump = null;     // open the go-to palette
    this.onViewMoved = null; // the buffer scrolled; scroll sync listens
    this._syncedLine = null; // the line the page is showing, while sync is on
    this._revealTimer = null;
    this._outlineTimer = null;
    this._placeTimer = null;
    this._placeKey = 'texai.editor.place';
    // Opens are async and can overlap; only the newest may touch the buffer.
    this._openToken = 0;

    this.outline = new OutlinePanel({
      panel: 'outline',
      list: 'outline-list',
      toggle: 'outline-toggle',
      storageKey: 'texai.outline',
      // A heading is a place in both panes, so go to it in both.
      onPick: (entry) => {
        this.goToLine(entry.line);
        this.onReveal?.(this.file, entry.line, entry.phrase ? [entry.phrase] : []);
      },
      onOpenFile: (target) => this.open(resolveInput(target, this.file, this.files || [])),
    });

    this._wire();
  }

  /* ---------------- setup ---------------- */

  _wire() {
    this.els.picker.addEventListener('change', () => {
      const next = this.els.picker.value;
      if (next && next !== this.file) this.open(next);
      else this.els.picker.value = this.file || '';
    });
    this.els.save.addEventListener('click', () => this.save());
    this.els.revert.addEventListener('click', () => {
      this._menu(false);
      this.reload({ force: true });
    });
    this.els.merged?.addEventListener('click', () => this._visitMerged());

    this.els.menu?.addEventListener('click', () => this._menu(this.els.menuPop.hidden));
    // Click-away and Escape, as the git panel closes.
    document.addEventListener('click', (event) => {
      if (this.els.menuPop?.hidden !== false) return;
      if (this.els.menu.contains(event.target) || this.els.menuPop.contains(event.target)) return;
      this._menu(false);
    });
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && this.els.menuPop?.hidden === false) this._menu(false);
    });
    this.els.search?.addEventListener('click', () => this.onJump?.());
  }

  _mount() {
    if (this.cm) return this.cm;
    if (!window.CodeMirror) {
      this._setStatus('The editor failed to load.', 'err');
      return null;
    }
    this.cm = window.CodeMirror(this.els.host, {
      mode: 'stex',
      theme: 'texai',
      lineNumbers: true,
      lineWrapping: true,
      matchBrackets: true,
      indentUnit: 2,
      tabSize: 2,
      value: '',
      extraKeys: {
        'Cmd-S': () => this.save(),
        'Ctrl-S': () => this.save(),
      },
    });
    this.cm.on('change', () => {
      if (this._loading) return;
      this._setDirty(true);
      // Re-read the outline from the buffer, but not on every keystroke.
      clearTimeout(this._outlineTimer);
      this._outlineTimer = setTimeout(() => this._refreshOutline(), 250);
    });

    this.cm.on('cursorActivity', () => this._cursorMoved());
    this.cm.on('scroll', () => this.onViewMoved?.());

    // Clicking in the source shows that spot in the PDF — the same trip as
    // Cmd-click, in the other direction. Bound to the click rather than to
    // cursor movement, so typing and arrow keys never yank the page around.
    this.cm.on('mousedown', (cm, event) => {
      if (event.button !== 0 || !this.onReveal) return;
      const where = cm.coordsChar({ left: event.clientX, top: event.clientY }, 'window');
      if (!where) return;
      // SyncTeX answers with the line; the words around the click are what get
      // the highlight down to the phrase you actually pointed at.
      const phrases = phraseAt(cm.getLine(where.line) || '', where.ch);
      // A double-click is two mousedowns; coalesce them into one lookup.
      clearTimeout(this._revealTimer);
      this._revealTimer = setTimeout(
        () => this.onReveal(this.file, where.line + 1, phrases),
        120
      );
    });

    return this.cm;
  }

  /** The project's editable files, fetched once. */
  async ensureFiles() {
    if (!this.filesLoaded) await this._loadFileList();
    return this.files || [];
  }

  async _loadFileList(select) {
    try {
      const data = await getJSON('/api/source/files');
      this.files = data.files || [];
      this.rootTex = data.rootTex || this.files[0] || null;
      this.els.picker.replaceChildren(
        ...this.files.map((file) => {
          const option = document.createElement('option');
          option.value = file;
          option.textContent = file;
          return option;
        })
      );
      this.filesLoaded = true;
      if (select && this.files.includes(select)) this.els.picker.value = select;
    } catch (err) {
      this._setStatus(err.message || String(err), 'err');
    }
  }

  /* ---------------- opening ---------------- */

  /** Show the source behind a spot in the document, at a line and column if given. */
  async open(file, line = null, column = null) {
    this._mount();
    if (!this.cm) return;

    if (this.dirty && this.file && file !== this.file) {
      const keep = !window.confirm(
        `${this.file} has unsaved edits. Discard them and open ${file}?`
      );
      if (keep) return;
    }

    const token = ++this._openToken;
    if (!this.filesLoaded) await this._loadFileList(file);
    if (token !== this._openToken) return;

    if (file !== this.file || !this.dirty) {
      try {
        const data = await getJSON(`/api/source?file=${encodeURIComponent(file)}`);
        // A newer open started while this one was in flight — that one owns the
        // buffer now, and writing this document would throw away its cursor.
        if (token !== this._openToken) return;
        this._setDoc(data);
      } catch (err) {
        if (token !== this._openToken) return;
        this._setStatus(err.message || String(err), 'err');
        return;
      }
    }

    if (this.files && !this.files.includes(this.file)) await this._loadFileList(this.file);
    this.els.picker.value = this.file;
    if (line != null) this.goToLine(line, column);
  }

  _setDoc({ file, text, sha }) {
    // Re-reading the file you are already in is a reload, not a new document:
    // the agent rewrote it, or you asked for its version. Where you were
    // reading is still where you want to be, so it survives the swap. A
    // different file starts at the top, as opening a file should.
    const keep = file === this.file ? this._place() : null;

    this._loading = true;
    this.cm.setValue(text);
    this.cm.clearHistory();
    this._loading = false;
    this.file = file;
    this.sha = sha;
    this.baseText = this.cm.getValue();
    this._clearMerged();
    this.clearSynced();
    this._setDirty(false);
    this._refreshOutline();
    this._setStatus(`${file} — ${this.cm.lineCount()} lines`);
    if (keep) this._restore(keep);
  }

  /* ---------------- keeping your place ---------------- */

  _place() {
    const cursor = this.cm.getCursor();
    return { line: cursor.line, ch: cursor.ch, top: this.cm.getScrollInfo().top };
  }

  /**
   * Put the cursor and the scroll back.
   *
   * The line is only as good as the file being roughly what it was: an agent
   * that inserted a paragraph above has moved everything down, and nothing
   * short of a diff would know by how much. Clamped, and quiet — no flash, no
   * scroll into the middle — because this is a restoration, not a jump.
   */
  _restore(place) {
    if (!this.cm || !place) return;
    const line = Math.max(0, Math.min(this.cm.lineCount() - 1, place.line || 0));
    const ch = Math.min(place.ch || 0, (this.cm.getLine(line) || '').length);
    this.cm.setCursor({ line, ch });
    if (place.top != null) this.cm.scrollTo(null, place.top);
  }

  /** Where you were, per project, so a browser refresh comes back to it. */
  setPlaceScope(scope) {
    this._placeKey = `texai.editor.place:${scope}`;
  }

  _rememberPlace() {
    if (!this.cm || !this.file) return;
    clearTimeout(this._placeTimer);
    this._placeTimer = setTimeout(() => {
      try {
        localStorage.setItem(
          this._placeKey,
          JSON.stringify({ file: this.file, ...this._place() })
        );
      } catch {
        /* storage disabled or full: the place is a convenience, not state */
      }
    }, 400);
  }

  _rememberedPlace() {
    try {
      const stored = JSON.parse(localStorage.getItem(this._placeKey) || 'null');
      return stored?.file ? stored : null;
    } catch {
      return null;
    }
  }

  _refreshOutline() {
    if (!this.cm) return;
    this.entries = parseOutline(this.cm.getValue());
    this.outline.setEntries(this.entries);
    this._cursorMoved();
  }

  /** One place for everything that follows the cursor: the outline, the memory. */
  _cursorMoved() {
    if (!this.cm) return;
    const line = this.cm.getCursor().line + 1;
    this.outline.setActiveLine(line);
    this._rememberPlace();
  }

  goToLine(line, column = null) {
    if (!this.cm) return;
    const index = Math.max(0, Math.min(this.cm.lineCount() - 1, Number(line) - 1));
    const text = this.cm.getLine(index) || '';
    const ch = column == null ? 0 : Math.max(0, Math.min(text.length, Number(column) - 1));
    this.cm.setCursor({ line: index, ch });

    // Given a column, select the word there — the cursor alone is easy to lose
    // in the middle of a long line.
    if (column != null) {
      const match = /^[\p{L}\p{N}][\p{L}\p{N}'-]*/u.exec(text.slice(ch));
      if (match) {
        this.cm.setSelection({ line: index, ch }, { line: index, ch: ch + match[0].length });
      }
    }
    // Put the line a third of the way down rather than at the very top, so
    // there is context above it.
    const top = this.cm.charCoords({ line: index, ch: 0 }, 'local').top;
    this.cm.scrollTo(null, Math.max(0, top - this.cm.getScrollInfo().clientHeight / 3));
    this.cm.addLineClass(index, 'background', 'cm-flash');
    setTimeout(() => this.cm?.removeLineClass(index, 'background', 'cm-flash'), 1600);
    this.cm.focus();
  }

  /* ---------------- following the page ---------------- */

  /** The first line of the buffer you can actually read, 1-based. */
  topVisibleLine() {
    if (!this.cm) return null;
    // A couple of pixels in, so a line scrolled half out of sight is not
    // mistaken for the one being read.
    return this.cm.lineAtHeight(this.cm.getScrollInfo().top + 2, 'local') + 1;
  }

  /**
   * Put a line near the top of the view, and take nothing else.
   *
   * The cursor, the selection and the focus all stay where they are: this is
   * the editor keeping up with the page, not you being sent somewhere. A file
   * with unsaved edits is never swapped out from under you, so a page showing
   * some other file simply leaves the buffer alone.
   */
  async showLine(file, line) {
    if (!file || !this.cm) return false;
    if (file !== this.file) {
      if (this.dirty) return false;
      await this.open(file);
      if (file !== this.file || !this.cm) return false;
    }
    const index = Math.max(0, Math.min(this.cm.lineCount() - 1, Number(line) - 1));
    const top = this.cm.charCoords({ line: index, ch: 0 }, 'local').top;
    this.cm.scrollTo(null, Math.max(0, top - this.cm.getScrollInfo().clientHeight * 0.18));
    this._markSynced(index);
    return true;
  }

  /** One quiet rule down the line the page is showing. */
  _markSynced(index) {
    this.clearSynced();
    if (this.cm) this._syncedLine = this.cm.addLineClass(index, 'background', 'cm-synced-line');
  }

  clearSynced() {
    if (this._syncedLine) {
      this.cm?.removeLineClass(this._syncedLine, 'background', 'cm-synced-line');
    }
    this._syncedLine = null;
  }

  /* ---------------- saving ---------------- */

  async save({ retried = false } = {}) {
    if (!this.cm || !this.file || this.saving) return;
    if (!this.dirty) {
      this._setStatus('Nothing to save.');
      return;
    }
    this.saving = true;
    this._setStatus('Saving and rebuilding…', 'busy');
    this.els.save.disabled = true;

    const text = this.cm.getValue();
    let clashed = false;
    try {
      const result = await postJSON('/api/source', {
        file: this.file,
        text,
        baseSha: this.sha,
      });
      this.sha = result.sha;
      this.baseText = text; // what is on disk now, and the base for the next merge
      // Only clear the flag if nothing was typed while the save was in flight.
      if (this.cm.getValue() === text) this._setDirty(false);
      // The highlight says "arrived since you last saved", and you just did.
      this._clearMerged();

      const build = result.build || {};
      if (build.ok) {
        this._setStatus(`Saved ${this.file} — ${build.summary || 'rebuilt'}`, 'ok');
      } else {
        this._setStatus(build.summary || 'Saved, but the build failed.', 'err');
        this._showErrors(build.errors || []);
      }
      this.onSaved?.(result);
    } catch (err) {
      clashed = err.code === 'source_conflict' && !retried;
      if (!clashed) this._setStatus(err.message || String(err), 'err');
    } finally {
      this.saving = false;
      this.els.save.disabled = false;
    }

    // Someone wrote to the file between this buffer loading and this save.
    // Nearly always somewhere else in it, so fold their version in and try
    // once more; only a real collision gets to interrupt anyone. Deliberately
    // outside the block above, so the retry starts from a settled editor.
    if (!clashed) return;
    const merged = await this.sync();
    if (!merged?.clean) {
      showToast('Your edits and the agent’s overlap. “Reload from disk” takes its version.', {
        type: 'error',
      });
      return;
    }
    if (!this.dirty) {
      this._setStatus('Nothing to save — the version on disk already has your edit.');
      return;
    }
    return this.save({ retried: true });
  }

  /* ---------------- keeping up with the other writer ---------------- */

  /**
   * Fold whatever is on disk now into the buffer.
   *
   * Called whenever the project may have been written to. The buffer, the text
   * it was loaded from, and the file on disk are three versions of one file;
   * the server merges them and answers with splices, which are applied here so
   * the cursor, the selection and the undo history all survive. Unsaved edits
   * are only in the way when the other writer touched the very same lines, and
   * then nothing is applied and the buffer is left alone.
   */
  async sync({ attempt = 0 } = {}) {
    if (!this.cm || !this.file || this.syncing) return null;
    this.syncing = true;
    const file = this.file;
    const sent = this.cm.getValue();
    let answer = null;
    let raced = false;
    try {
      const result = await postJSON('/api/source/merge', {
        file,
        text: sent,
        baseText: this.baseText,
      });
      if (this.cm && file === this.file) {
        // Typing that raced the round trip leaves the offsets pointing at the
        // wrong characters, so the answer is thrown away rather than applied.
        if (this.cm.getValue() !== sent) raced = true;
        else answer = result;
      }
    } catch (err) {
      // A file that vanished, or a server that is gone: neither is worth
      // interrupting typing over, and the next save will say so plainly.
      this._setStatus(err.message || String(err), 'err');
    } finally {
      this.syncing = false;
    }

    // Ask again with what the buffer holds now. A few keystrokes cannot keep
    // this up for long, and the attempt count stops a fast typist looping.
    if (raced) return attempt < 3 ? this.sync({ attempt: attempt + 1 }) : null;
    if (!answer || !answer.changed) return answer; // the file has not moved
    if (answer.clean) this._applyMerge(answer);
    else {
      this._setStatus(
        `${file} changed on disk where you are editing — ` +
          '“Reload from disk”, under the ⋮, takes that version.',
        'err'
      );
    }
    return answer;
  }

  /** Put the merged-in text into the buffer and colour what arrived. */
  _applyMerge({ base, sha, edits }) {
    const cm = this.cm;

    // Where each splice ends up once the ones before it have shifted the text.
    let delta = 0;
    const regions = edits.map(({ start, end, text }) => {
      const from = start + delta;
      delta += text.length - (end - start);
      return { from, to: from + text.length };
    });

    // Text arriving above the viewport would otherwise slide the page under
    // the reader; keep the line the cursor is on where it is on screen. Only
    // worth doing while the pane is visible — CodeMirror measures nothing
    // while it is hidden, and would answer with zeros.
    const anchor = this.active
      ? cm.cursorCoords(cm.getCursor(), 'local').top - cm.getScrollInfo().top
      : null;

    this._loading = true; // the change handler's dirty flag and outline are set below
    cm.operation(() => {
      // Applied last first, so the offsets of the earlier splices still hold.
      for (let i = edits.length - 1; i >= 0; i -= 1) {
        const { start, end, text } = edits[i];
        cm.replaceRange(text, cm.posFromIndex(start), cm.posFromIndex(end), 'merge');
      }
    });
    this._loading = false;

    if (anchor != null) {
      cm.scrollTo(null, Math.max(0, cm.cursorCoords(cm.getCursor(), 'local').top - anchor));
    }

    this.baseText = base;
    this.sha = sha;
    this._setDirty(cm.getValue() !== base);
    this._markMerged(regions);
    this._refreshOutline();
  }

  /* ---------------- what the other writer changed ---------------- */

  _markMerged(regions) {
    // Nothing arrived that was not already typed here: say nothing about it.
    if (!regions.length) return;
    for (const region of regions) {
      const first = this.cm.posFromIndex(region.from).line;
      // A deletion leaves no lines to colour; mark the seam it closed instead.
      const last = this.cm.posFromIndex(Math.max(region.to - 1, region.from)).line;
      for (let line = first; line <= last; line += 1) {
        this._mergedLines.push(this.cm.addLineClass(line, 'background', 'cm-merged-line'));
      }
      this._mergedSpots.push(this.cm.getLineHandle(first));
    }
    // What just arrived, which is not the same as the count on the chip: that
    // one is everything since the last save.
    this._setStatus(
      regions.length === 1
        ? 'Merged one change from disk — your edits are untouched.'
        : `Merged ${regions.length} changes from disk — your edits are untouched.`
    );
    this._showMerged();
  }

  _showMerged() {
    const chip = this.els.merged;
    if (!chip) return;
    const count = this._mergedSpots.length;
    chip.hidden = count === 0;
    chip.textContent = `${count} merged`;
    chip.title =
      `${count} change${count === 1 ? '' : 's'} written by someone else arrived here ` +
      'since your last save. Click to visit them.';
  }

  _clearMerged() {
    for (const line of this._mergedLines) {
      this.cm?.removeLineClass(line, 'background', 'cm-merged-line');
    }
    this._mergedLines = [];
    this._mergedSpots = [];
    this._mergedAt = 0;
    if (this.els.merged) this.els.merged.hidden = true;
  }

  /** Step through the merged-in changes, one per click. */
  _visitMerged() {
    if (!this.cm || !this._mergedSpots.length) return;
    // A line that was deleted since the merge has no number any more; skip it
    // rather than land somewhere arbitrary.
    for (let i = 0; i < this._mergedSpots.length; i += 1) {
      const spot = this._mergedSpots[this._mergedAt % this._mergedSpots.length];
      this._mergedAt = (this._mergedAt + 1) % this._mergedSpots.length;
      const line = this.cm.getLineNumber(spot);
      if (line != null) {
        this.goToLine(line + 1);
        return;
      }
    }
  }

  /**
   * Pick up what is on disk.
   *
   * Unforced this is a merge, which keeps unsaved work; the Reload button
   * forces it, which is the way to take the other version outright after a
   * collision — and the only path that can throw an edit away.
   */
  async reload({ force = false } = {}) {
    if (!this.file || !this.cm) return;
    if (!force) {
      await this.sync();
      return;
    }
    if (this.dirty && !window.confirm(`Discard your unsaved edits to ${this.file}?`)) {
      return;
    }
    try {
      this._setDoc(await getJSON(`/api/source?file=${encodeURIComponent(this.file)}`));
    } catch (err) {
      this._setStatus(err.message || String(err), 'err');
    }
  }

  /* ---------------- state ---------------- */

  /** The overflow menu, which is where anything that can lose work lives. */
  _menu(open) {
    const pop = this.els.menuPop;
    if (!pop) return;
    const hadFocus = pop.contains(document.activeElement);
    pop.hidden = !open;
    this.els.menu.setAttribute('aria-expanded', String(open));
    this.els.menu.classList.toggle('on', open);
    // Open it and the item is under the keyboard as well as the pointer;
    // close it and the buffer gets the keyboard back.
    if (open) this.els.revert.focus();
    else if (hadFocus) this.focus();
  }

  _setDirty(value) {
    this.dirty = value;
    this.els.dirty.hidden = !value;
    this.els.save.classList.toggle('primary', value);
    if (value) this._clearErrors();
  }

  _setStatus(text, kind = '') {
    this.els.status.textContent = text;
    this.els.status.className = `editor-status ${kind}`;
  }

  _clearErrors() {
    if (!this.cm) return;
    for (const line of this._errorLines || []) {
      this.cm.removeLineClass(line, 'background', 'cm-error-line');
    }
    this._errorLines = [];
  }

  /** Mark the lines LaTeX complained about, when they are in this file.
   *
   * The log gives a line number but not a file, and that number belongs to
   * whichever file TeX was reading — which may not be this one. It also quotes
   * the source text it choked on, so we only mark a line when that text is
   * really there. An unmarked error still shows in the status bar.
   */
  _showErrors(errors) {
    this._clearErrors();
    if (!this.cm) return;
    this._errorLines = [];

    for (const error of errors) {
      const match = /\(line (\d+)\)(.*)$/.exec(String(error));
      if (!match) continue;
      const line = Number(match[1]);
      if (line < 1 || line > this.cm.lineCount()) continue;

      const quoted = match[2].trim();
      if (quoted.length < 3) continue; // nothing to check it against
      if (!(this.cm.getLine(line - 1) || '').includes(quoted)) continue;

      this.cm.addLineClass(line - 1, 'background', 'cm-error-line');
      this._errorLines.push(line - 1);
    }
    if (this._errorLines.length) this.goToLine(this._errorLines[0] + 1);
  }

  /** Take the keyboard back, but only while the editor is the visible tab. */
  focus() {
    if (this.active) this.cm?.focus();
  }

  setActive(active) {
    this.active = active;
    this.onActivate?.(active);
    if (!active) {
      this._menu(false); // it would otherwise be waiting, open, on the way back
      return;
    }
    this._mount();
    if (!this.filesLoaded) this._loadFileList().then(() => this._openDefault());
    else if (!this.file) this._openDefault();
    // Coming back to a file the agent has been working in: catch up before the
    // first keystroke lands on top of a version that has moved on.
    else this.sync();
    // CodeMirror measures nothing while hidden, so it must be told to remeasure.
    setTimeout(() => this.cm?.refresh(), 0);
  }

  _openDefault() {
    // Deferred behind a file-list fetch, so by now an explicit open may have
    // been asked for — opening the root file over it would drop its cursor.
    if (this._openToken > 0) return;

    // A refresh should not cost you your place. The remembered file is only
    // honoured if the project still has it, so opening a different project
    // falls back to its own root .tex rather than to a stranger's file.
    const place = this._rememberedPlace();
    if (place && this.files?.includes(place.file)) {
      this.open(place.file).then(() => {
        // An explicit open may have overtaken this one while it was in flight.
        if (this.file === place.file) this._restore(place);
      });
      return;
    }
    const first = this.rootTex || this.files?.[0];
    if (first) this.open(first);
  }
}
