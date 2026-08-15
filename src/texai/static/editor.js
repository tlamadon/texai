// A small LaTeX editor over the same project the agent edits.
//
// Two writers on one tree, so every save carries the hash of the text it was
// based on: if the agent rewrote the file underneath, the save is refused
// rather than silently discarding its work. Saving rebuilds the PDF, and the
// watcher in app.js reloads the page as it would after any other build.

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
      status: document.getElementById('editor-status'),
      dirty: document.getElementById('editor-dirty'),
      search: document.getElementById('outline-search'),
      tab: document.getElementById('tab-source'),
    };

    this.cm = null;
    this.file = null;
    this.sha = null;
    this.dirty = false;
    this.saving = false;
    this.active = false;
    this.filesLoaded = false;
    this.entries = [];
    this.onSaved = onSaved || null;
    this.onActivate = null; // app.js hides the composer while editing
    this.onReveal = null;   // show this source line in the PDF
    this.onJump = null;     // open the go-to palette
    this._revealTimer = null;
    this._outlineTimer = null;
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
    this.els.revert.addEventListener('click', () => this.reload({ force: true }));
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
    this._loading = true;
    this.cm.setValue(text);
    this.cm.clearHistory();
    this._loading = false;
    this.file = file;
    this.sha = sha;
    this._setDirty(false);
    this._refreshOutline();
    this._setStatus(`${file} — ${this.cm.lineCount()} lines`);
  }

  _refreshOutline() {
    if (!this.cm) return;
    this.entries = parseOutline(this.cm.getValue());
    this.outline.setEntries(this.entries);
    this._cursorMoved();
  }

  /** One place for everything that follows the cursor: the outline, the page. */
  _cursorMoved() {
    if (!this.cm) return;
    const line = this.cm.getCursor().line + 1;
    this.outline.setActiveLine(line);
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

  /* ---------------- saving ---------------- */

  async save() {
    if (!this.cm || !this.file || this.saving) return;
    if (!this.dirty) {
      this._setStatus('Nothing to save.');
      return;
    }
    this.saving = true;
    this._setStatus('Saving and rebuilding…', 'busy');
    this.els.save.disabled = true;

    const text = this.cm.getValue();
    try {
      const result = await postJSON('/api/source', {
        file: this.file,
        text,
        baseSha: this.sha,
      });
      this.sha = result.sha;
      // Only clear the flag if nothing was typed while the save was in flight.
      if (this.cm.getValue() === text) this._setDirty(false);

      const build = result.build || {};
      if (build.ok) {
        this._setStatus(`Saved ${this.file} — ${build.summary || 'rebuilt'}`, 'ok');
      } else {
        this._setStatus(build.summary || 'Saved, but the build failed.', 'err');
        this._showErrors(build.errors || []);
      }
      this.onSaved?.(result);
    } catch (err) {
      const message = err.message || String(err);
      // A conflict means the agent got there first — offer the newer text.
      if (/changed since you opened it/i.test(message)) {
        this._setStatus(message, 'err');
        showToast('The file changed on disk. Use “Reload” to pick up the new version.', {
          type: 'error',
        });
      } else {
        this._setStatus(message, 'err');
      }
    } finally {
      this.saving = false;
      this.els.save.disabled = false;
    }
  }

  /** Pick up what is on disk; refuses to discard unsaved work unless forced. */
  async reload({ force = false } = {}) {
    if (!this.file || !this.cm) return;
    if (this.dirty && !force) return;
    if (this.dirty && force && !window.confirm(`Discard your unsaved edits to ${this.file}?`)) {
      return;
    }
    try {
      this._setDoc(await getJSON(`/api/source?file=${encodeURIComponent(this.file)}`));
    } catch (err) {
      this._setStatus(err.message || String(err), 'err');
    }
  }

  /* ---------------- state ---------------- */

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
    if (!active) return;
    this._mount();
    if (!this.filesLoaded) this._loadFileList().then(() => this._openDefault());
    else if (!this.file) this._openDefault();
    // CodeMirror measures nothing while hidden, so it must be told to remeasure.
    setTimeout(() => this.cm?.refresh(), 0);
  }

  _openDefault() {
    // Deferred behind a file-list fetch, so by now an explicit open may have
    // been asked for — opening the root file over it would drop its cursor.
    if (this._openToken > 0) return;
    const first = this.rootTex || this.files?.[0];
    if (first) this.open(first);
  }
}
