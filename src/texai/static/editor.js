// A small LaTeX editor over the same project the agent edits.
//
// Two writers on one tree, so every save carries the hash of the text it was
// based on: if the agent rewrote the file underneath, the save is refused
// rather than silently discarding its work. Saving rebuilds the PDF, and the
// watcher in app.js reloads the page as it would after any other build.

import { getJSON, postJSON } from './api.js';
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
      tab: document.getElementById('tab-source'),
    };

    this.cm = null;
    this.file = null;
    this.sha = null;
    this.dirty = false;
    this.saving = false;
    this.active = false;
    this.filesLoaded = false;
    this.onSaved = onSaved || null;
    this.onActivate = null; // app.js hides the composer while editing

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
    });
    return this.cm;
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

  /** Show the source behind a spot in the document, at a line if given. */
  async open(file, line = null) {
    this._mount();
    if (!this.cm) return;

    if (this.dirty && this.file && file !== this.file) {
      const keep = !window.confirm(
        `${this.file} has unsaved edits. Discard them and open ${file}?`
      );
      if (keep) return;
    }

    if (!this.filesLoaded) await this._loadFileList(file);

    if (file !== this.file || !this.dirty) {
      try {
        const data = await getJSON(`/api/source?file=${encodeURIComponent(file)}`);
        this._setDoc(data);
      } catch (err) {
        this._setStatus(err.message || String(err), 'err');
        return;
      }
    }

    if (this.files && !this.files.includes(this.file)) await this._loadFileList(this.file);
    this.els.picker.value = this.file;
    if (line != null) this.goToLine(line);
  }

  _setDoc({ file, text, sha }) {
    this._loading = true;
    this.cm.setValue(text);
    this.cm.clearHistory();
    this._loading = false;
    this.file = file;
    this.sha = sha;
    this._setDirty(false);
    this._setStatus(`${file} — ${this.cm.lineCount()} lines`);
  }

  goToLine(line) {
    if (!this.cm) return;
    const index = Math.max(0, Math.min(this.cm.lineCount() - 1, Number(line) - 1));
    this.cm.setCursor({ line: index, ch: 0 });
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
    const first = this.rootTex || this.files?.[0];
    if (first) this.open(first);
  }
}
