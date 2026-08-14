// Git for the project, in one pill and one panel.
//
// The pill carries the two numbers worth glancing at — uncommitted files, and
// commits waiting to be pushed. Everything else lives behind a click.
//
// Commits are never made from a message you have not seen: the agent proposes
// one, it lands in an editable box, and only then does Commit do anything.

import { getJSON, postJSON } from './api.js';
import { showToast } from './toast.js';

const REFRESH_MS = 10000;

export class GitPanel {
  constructor() {
    this.els = {
      pill: document.getElementById('git-pill'),
      branch: document.getElementById('git-branch'),
      count: document.getElementById('git-count'),
      ahead: document.getElementById('git-ahead'),
      panel: document.getElementById('git-panel'),
      where: document.getElementById('git-where'),
      files: document.getElementById('git-files'),
      compose: document.getElementById('git-compose'),
      message: document.getElementById('git-message'),
      messageNote: document.getElementById('git-message-note'),
      commit: document.getElementById('git-commit'),
      confirm: document.getElementById('git-confirm'),
      cancel: document.getElementById('git-cancel'),
      pull: document.getElementById('git-pull'),
      push: document.getElementById('git-push'),
      output: document.getElementById('git-output'),
    };

    this.status = null;
    this.busy = false;
    this.open = false;
    this._wire();
  }

  _wire() {
    this.els.pill.addEventListener('click', () => this.toggle());
    this.els.commit.addEventListener('click', () => this.startCommit());
    this.els.confirm.addEventListener('click', () => this.finishCommit());
    this.els.cancel.addEventListener('click', () => this._closeCompose());
    this.els.pull.addEventListener('click', () => this.run('pull', 'Pulling…'));
    this.els.push.addEventListener('click', () => this.run('push', 'Pushing…'));

    // Click-away and Escape, but never while a git command is in flight.
    document.addEventListener('click', (event) => {
      if (!this.open) return;
      if (this.els.panel.contains(event.target) || this.els.pill.contains(event.target)) return;
      this.toggle(false);
    });
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && this.open) this.toggle(false);
    });
  }

  async start() {
    await this.refresh();
    setInterval(() => {
      // Someone may be committing in a terminal; do not fight an in-flight action.
      if (!this.busy) this.refresh();
    }, REFRESH_MS);
  }

  toggle(force = null) {
    this.open = force === null ? !this.open : force;
    this.els.panel.hidden = !this.open;
    this.els.pill.classList.toggle('active', this.open);
    if (this.open) {
      this._closeCompose();
      // Opening the panel is when the remote's state actually matters.
      this.refresh({ fetch: true });
    }
  }

  /* ---------------- status ---------------- */

  async refresh({ fetch = false } = {}) {
    try {
      this.status = await getJSON(`/api/git/status${fetch ? '?fetch=1' : ''}`);
    } catch {
      this.status = null;
    }
    this._render();
  }

  _render() {
    const s = this.status;
    // Not a repository is not a failure — there is simply nothing to show.
    this.els.pill.hidden = !s || !s.repo;
    if (!s || !s.repo) {
      this.toggle(false);
      return;
    }

    this.els.branch.textContent = s.detached ? 'detached' : s.branch || '—';
    this.els.count.textContent = String(s.dirty);
    this.els.count.hidden = s.dirty === 0;
    this.els.count.classList.toggle('conflict', s.conflicted);
    this.els.ahead.textContent = `↑${s.ahead}`;
    this.els.ahead.hidden = s.ahead === 0;
    this.els.pill.title = this._pillTitle(s);

    this.els.where.textContent = this._where(s);
    this._renderFiles(s);

    const canCommit = s.dirty > 0 && !s.conflicted && !this.busy;
    this.els.commit.disabled = !canCommit;
    this.els.commit.textContent = s.dirty ? `Commit ${s.dirty} file${s.dirty === 1 ? '' : 's'}…` : 'Nothing to commit';
    this.els.pull.disabled = this.busy || !s.upstream;
    this.els.pull.textContent = s.behind ? `Pull ${s.behind} ↓` : 'Pull';
    this.els.pull.title = s.upstream
      ? `git pull --rebase (${s.upstream})`
      : 'This branch has no upstream to pull from.';
    this.els.push.disabled = this.busy || s.ahead === 0;
    this.els.push.textContent = s.ahead ? `Push ${s.ahead} ↑` : 'Push';
    this.els.push.title = s.upstream
      ? `git push to ${s.upstream}`
      : 'No upstream yet — pushing will set one.';
  }

  _pillTitle(s) {
    const bits = [s.detached ? 'detached HEAD' : `on ${s.branch}`];
    bits.push(s.dirty ? `${s.dirty} uncommitted file${s.dirty === 1 ? '' : 's'}` : 'nothing uncommitted');
    if (s.ahead) bits.push(`${s.ahead} to push`);
    if (s.behind) bits.push(`${s.behind} to pull`);
    if (s.conflicted) bits.push('unresolved conflicts');
    return bits.join(' · ');
  }

  _where(s) {
    const parts = [];
    if (s.upstream) parts.push(`tracking ${s.upstream}`);
    else parts.push('no upstream');
    // The root is often a subdirectory of a bigger repo, and that changes what
    // a commit here means. Say so rather than let it surprise anyone.
    if (s.scoped) parts.push('only files under the project root are committed');
    return parts.join(' · ');
  }

  _renderFiles(s) {
    const list = this.els.files;
    list.replaceChildren();
    if (!s.files.length) {
      const empty = document.createElement('div');
      empty.className = 'git-empty';
      empty.textContent = 'Nothing uncommitted.';
      list.append(empty);
      return;
    }
    for (const file of s.files) {
      const row = document.createElement('div');
      row.className = 'git-file';
      const state = document.createElement('span');
      state.className = `git-state ${file.state}`;
      state.textContent = file.state[0].toUpperCase();
      state.title = file.state;
      const name = document.createElement('span');
      name.className = 'git-path';
      name.textContent = file.path;
      row.append(state, name);
      list.append(row);
    }
    if (s.truncated) {
      const more = document.createElement('div');
      more.className = 'git-empty';
      more.textContent = `…and more (${s.dirty} in total).`;
      list.append(more);
    }
  }

  /* ---------------- committing ---------------- */

  async startCommit() {
    if (this.busy) return;
    this._setBusy(true, 'Writing a commit message…');
    try {
      const proposed = await postJSON('/api/git/message', {});
      this.els.message.value = proposed.message || '';
      this.els.messageNote.textContent =
        proposed.source === 'agent'
          ? 'Written by the agent — edit it if you like.'
          : `Fallback message: ${proposed.reason || 'the agent could not write one.'}`;
      this.els.messageNote.classList.toggle('warn', proposed.source !== 'agent');
      this.els.compose.hidden = false;
      this._setOutput('');
      this.els.message.focus();
      // Cursor at the end of the subject line, where edits usually go.
      const firstLine = this.els.message.value.indexOf('\n');
      const end = firstLine === -1 ? this.els.message.value.length : firstLine;
      this.els.message.setSelectionRange(end, end);
    } catch (err) {
      this._setOutput(err.message || String(err), 'err');
    } finally {
      this._setBusy(false);
    }
  }

  async finishCommit() {
    const message = this.els.message.value.trim();
    if (!message) {
      this._setOutput('A commit needs a message.', 'err');
      return;
    }
    this._setBusy(true, 'Committing…');
    try {
      const result = await postJSON('/api/git/commit', { message });
      this._closeCompose();
      this.status = result.status;
      showToast(`Committed ${result.commit} — ${result.subject}`);
      this._setOutput(`${result.commit} ${result.subject}`, 'ok');
    } catch (err) {
      this._setOutput(err.message || String(err), 'err');
    } finally {
      this._setBusy(false);
      this._render();
    }
  }

  _closeCompose() {
    this.els.compose.hidden = true;
    this.els.message.value = '';
    this.els.messageNote.textContent = '';
  }

  /* ---------------- pull & push ---------------- */

  async run(action, label) {
    if (this.busy) return;
    this._setBusy(true, label);
    try {
      const result = await postJSON(`/api/git/${action}`, {});
      this.status = result.status;
      const text = (result.output || '').trim();
      this._setOutput(text || 'Done.', 'ok');
      showToast(action === 'pull' ? 'Pulled and rebased' : 'Pushed');
    } catch (err) {
      this._setOutput(err.message || String(err), 'err');
    } finally {
      this._setBusy(false);
      this._render();
    }
  }

  /* ---------------- state ---------------- */

  _setBusy(busy, label = '') {
    this.busy = busy;
    this.els.panel.classList.toggle('busy', busy);
    for (const button of [this.els.commit, this.els.pull, this.els.push, this.els.confirm]) {
      button.disabled = busy;
    }
    if (busy && label) this._setOutput(label, 'busy');
    if (!busy) this._render();
  }

  _setOutput(text, kind = '') {
    this.els.output.textContent = text;
    this.els.output.className = `git-output ${kind}`;
    this.els.output.hidden = !text;
  }
}
