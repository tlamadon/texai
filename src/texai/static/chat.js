// The chat panel: selection chips, the composer, and the live agent stream.
//
// Two views over one stream of transcript entries. "Chat" renders a friendly
// summary per turn; "Transcript" renders the same entries verbatim, the way a
// terminal session would. Building both from one stream means the transcript
// cannot show something the chat quietly dropped.

import { getJSON, postJSON } from './api.js';
import { showToast } from './toast.js';

const MAX_CHIP_TEXT = 90;
const MAX_DETAIL_LINES = 20;

const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};

const truncate = (text, limit) =>
  text && text.length > limit ? `${text.slice(0, limit - 1)}…` : text || '';

/** A `file:line` label that scrolls the PDF to that spot when clicked. */
function refNode(file, line, onGoTo, label) {
  const node = el('span', 'file ref', label ?? `${file}:${line}`);
  node.title = `Show ${file}:${line} in the PDF`;
  node.addEventListener('click', (event) => {
    event.stopPropagation();
    onGoTo?.(file, line);
  });
  return node;
}

export class ChatPanel {
  constructor() {
    this.els = {
      messages: document.getElementById('messages'),
      transcript: document.getElementById('transcript'),
      placeholder: document.getElementById('chat-placeholder'),
      chips: document.getElementById('chips'),
      input: document.getElementById('composer-input'),
      send: document.getElementById('send'),
      clear: document.getElementById('clear-chips'),
      hint: document.getElementById('composer-hint'),
      status: document.getElementById('agent-status'),
      interrupt: document.getElementById('interrupt'),
      tabChat: document.getElementById('tab-chat'),
      tabTranscript: document.getElementById('tab-transcript'),
      attach: document.getElementById('attach-terminal'),
    };

    this.chips = [];
    this.cards = new Map(); // turnId -> {root, body, badge, footer}
    this.agent = { available: false, reason: null };
    this.busy = false;
    this.lastEventId = 0;
    this.view = 'chat';
    this.lastTranscriptTurn = null;
    this.onTurnFinished = null;
    this.onGoTo = null;      // click a file:line reference
    this.onNavigate = null;  // the agent moved the view itself

    this._wire();
  }

  /* ---------------- wiring ---------------- */

  _wire() {
    this.els.send.addEventListener('click', () => this.send());
    this.els.clear.addEventListener('click', () => this.clearChips());
    this.els.interrupt.addEventListener('click', () => this.interrupt());
    this.els.tabChat.addEventListener('click', () => this.setView('chat'));
    this.els.tabTranscript.addEventListener('click', () => this.setView('transcript'));
    this.els.attach.addEventListener('click', () => this.copyResumeCommand());
    this.els.input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        this.send();
      }
    });
  }

  setView(view) {
    this.view = view;
    const isChat = view === 'chat';
    this.els.messages.hidden = !isChat;
    this.els.transcript.hidden = isChat;
    this.els.tabChat.classList.toggle('active', isChat);
    this.els.tabTranscript.classList.toggle('active', !isChat);
    if (!isChat) this._scrollTranscript();
  }

  async start() {
    try {
      const info = await getJSON('/api/info');
      this.agent = info.agent || { available: false, reason: 'unknown' };
      this._renderStatus();
      this._renderAttach();
    } catch {
      this._renderStatus();
    }

    try {
      const history = await getJSON('/api/turns');
      for (const turn of history.turns) {
        this._renderCompletedTurn(turn);
        for (const entry of turn.transcript || []) this._appendTranscript(turn.id, entry);
        this._appendTurnRule(turn);
      }
      this._setBusy(history.busy);
      const latest = [...history.turns].reverse().find((t) => t.hunks?.length);
      if (latest) this.onTurnFinished?.(latest);
    } catch {
      /* no history yet */
    }

    this._connect();
  }

  /* ---------------- selection chips ---------------- */

  addSelection(selection) {
    const source = selection.source || {};
    const chip = {
      id: `c${Date.now()}${this.chips.length}`,
      file: source.file,
      line: source.line,
      column: source.column ?? 1,
      page: selection.page ?? null,
      selectedText: selection.selectedText ?? null,
      instruction: '',
    };
    if (!chip.file || !chip.line) return;

    // Re-clicking the same line replaces rather than duplicates.
    const existing = this.chips.findIndex((c) => c.file === chip.file && c.line === chip.line);
    if (existing >= 0) {
      chip.instruction = this.chips[existing].instruction;
      this.chips[existing] = chip;
    } else {
      this.chips.push(chip);
    }
    this._renderChips();
    this.els.input.focus();
  }

  clearChips() {
    this.chips = [];
    this._renderChips();
  }

  _renderChips() {
    this.els.chips.replaceChildren();

    for (const chip of this.chips) {
      const node = el('div', 'chip');

      const head = el('div', 'chip-head');
      const ref = el('span', 'chip-ref ref', `${chip.file}:${chip.line}`);
      ref.title = 'Show this passage in the PDF';
      ref.addEventListener('click', () => this.onGoTo?.(chip.file, chip.line));
      head.append(ref);
      head.append(
        el('span', 'chip-text', chip.selectedText ? `“${truncate(chip.selectedText, MAX_CHIP_TEXT)}”` : '')
      );
      const remove = el('button', 'chip-remove', '×');
      remove.title = 'Remove this passage';
      remove.addEventListener('click', () => {
        this.chips = this.chips.filter((c) => c.id !== chip.id);
        this._renderChips();
      });
      head.append(remove);
      node.append(head);

      const note = el('input');
      note.type = 'text';
      note.placeholder = 'What should change here? (optional)';
      note.value = chip.instruction;
      note.addEventListener('input', () => {
        chip.instruction = note.value;
      });
      node.append(note);

      this.els.chips.append(node);
    }

    this.els.clear.hidden = this.chips.length === 0;
    this._renderHint();
  }

  _renderHint() {
    const n = this.chips.length;
    this.els.hint.textContent = n === 0
      ? 'Cmd/Ctrl-click the PDF to attach a passage'
      : `${n} passage${n === 1 ? '' : 's'} attached`;
  }

  /* ---------------- sending ---------------- */

  async send() {
    if (this.busy) {
      showToast('The agent is still working on the previous message.', { type: 'error' });
      return;
    }
    const message = this.els.input.value.trim();
    if (!message && this.chips.length === 0) return;

    if (!this.agent.available) {
      showToast(this.agent.reason || 'The agent is not available.', { type: 'error' });
      return;
    }

    const selections = this.chips.map((chip) => ({
      file: chip.file,
      line: chip.line,
      column: chip.column,
      page: chip.page,
      selectedText: chip.selectedText,
      instruction: chip.instruction.trim() || null,
    }));

    this._setBusy(true);
    try {
      await postJSON('/api/chat', { message, selections });
      this.els.input.value = '';
      this.clearChips();
    } catch (err) {
      this._setBusy(false);
      showToast(err.message || String(err), { type: 'error' });
    }
  }

  async interrupt() {
    try {
      await postJSON('/api/interrupt', {});
      showToast('Asked the agent to stop.');
    } catch (err) {
      showToast(err.message || String(err), { type: 'error' });
    }
  }

  async copyResumeCommand() {
    const command = this.agent.resumeCommand;
    if (!command) return;
    try {
      await navigator.clipboard.writeText(command);
      showToast(`Copied. Run it in ${'`'}--root${'`'} to attach your terminal:\n${command}`);
    } catch {
      showToast(`Run this in your project directory:\n${command}`);
    }
  }

  _setBusy(busy) {
    this.busy = busy;
    this.els.send.disabled = busy;
    this.els.interrupt.hidden = !busy;
    this._renderStatus();
  }

  _renderStatus() {
    const status = this.els.status;
    status.classList.remove('ok', 'busy', 'off');
    if (!this.agent.available) {
      status.textContent = 'disabled';
      status.title = this.agent.reason || '';
      status.classList.add('off');
    } else if (this.busy) {
      status.textContent = 'working…';
      status.classList.add('busy');
    } else {
      status.textContent = 'ready';
      status.classList.add('ok');
    }
  }

  _renderAttach() {
    const command = this.agent.resumeCommand;
    this.els.attach.hidden = !command;
    if (command) this.els.attach.title = `Copy: ${command}`;
  }

  /* ---------------- event stream ---------------- */

  _connect() {
    const source = new EventSource(`/api/events?since=${this.lastEventId}`);
    source.onmessage = (event) => {
      let payload;
      try {
        payload = JSON.parse(event.data);
      } catch {
        return;
      }
      this.lastEventId = payload.id ?? this.lastEventId;
      this._handle(payload);
    };
    source.onerror = () => {
      // EventSource would reconnect on its own but replay from `since=0`;
      // close and reopen with the id we actually reached.
      source.close();
      setTimeout(() => this._connect(), 1500);
    };
  }

  _handle(event) {
    switch (event.type) {
      case 'turn_started':
        this._setBusy(true);
        this._cardFor(event.turn);
        break;
      case 'agent_entry':
        this._appendTranscript(event.turnId, event.entry);
        this._appendToCard(event.turnId, event.entry);
        break;
      case 'turn_finished':
        this._finishCard(event.turn);
        this._appendTurnRule(event.turn);
        this._setBusy(false);
        this.onTurnFinished?.(event.turn);
        break;
      case 'agent_ready':
        this.agent = { ...this.agent, available: true, reason: null };
        this._renderStatus();
        break;
      case 'navigate':
        this.onNavigate?.(event);
        break;
      case 'agent_session':
        this.agent = {
          ...this.agent,
          sessionId: event.sessionId,
          resumeCommand: event.resumeCommand,
        };
        this._renderAttach();
        break;
      default:
        break;
    }
  }

  /* ---------------- transcript view ---------------- */

  _appendTranscript(turnId, entry) {
    const pre = this.els.transcript;
    if (this.lastTranscriptTurn !== turnId) {
      this.lastTranscriptTurn = turnId;
      pre.append(el('span', 't-rule', `\n──── ${turnId} ────\n`));
    }

    const push = (cls, text) => pre.append(el('span', cls, text));
    const errored = entry.isError ? ' t-err' : '';

    switch (entry.kind) {
      case 'user':
        push('t-user', `\n› ${entry.text}\n`);
        break;
      case 'text':
        push('t-text', `\n● ${entry.text}\n`);
        break;
      case 'thinking':
        push('t-thinking', `  ✻ ${entry.text}\n`);
        break;
      case 'tool_use':
        push(`t-tool${errored}`, `\n⏵ ${entry.verb}  ${entry.text || ''}\n`);
        break;
      case 'tool_result':
        push(`t-result${errored}`, `  ⎿ ${entry.text || ''}\n`);
        break;
      case 'build':
        push(`t-build${errored}`, `\n⚙ build  ${entry.text}\n`);
        break;
      case 'result':
        push('t-meta', `\n✓ ${entry.text}\n`);
        break;
      default:
        push(`t-notice${errored}`, `  ${entry.text || ''}\n`);
        break;
    }

    if (entry.detail) {
      const lines = entry.detail.split('\n');
      const shown = lines.slice(0, MAX_DETAIL_LINES).map((l) => `    ${l}`).join('\n');
      const rest = lines.length > MAX_DETAIL_LINES ? `\n    … ${lines.length - MAX_DETAIL_LINES} more lines` : '';
      push('t-detail', `${shown}${rest}\n`);
    }

    this._scrollTranscript();
  }

  _appendTurnRule(turn) {
    const bits = [turn.status];
    if (turn.changes?.length) {
      const added = turn.changes.reduce((n, c) => n + c.added, 0);
      const removed = turn.changes.reduce((n, c) => n + c.removed, 0);
      bits.push(`${turn.changes.length} file${turn.changes.length === 1 ? '' : 's'}`, `+${added}/−${removed}`);
    }
    if (turn.costUsd) bits.push(`$${turn.costUsd.toFixed(4)}`);
    this.els.transcript.append(
      el('span', 't-rule', `──── ${turn.id} ${bits.join(' · ')} ────\n`)
    );
    this._scrollTranscript();
  }

  _scrollTranscript() {
    if (this.view === 'transcript') {
      this.els.transcript.scrollTop = this.els.transcript.scrollHeight;
    }
  }

  /* ---------------- chat cards ---------------- */

  _cardFor(turn) {
    if (this.cards.has(turn.id)) return this.cards.get(turn.id);
    if (this.els.placeholder) this.els.placeholder.remove();

    const root = el('div', 'turn');

    const head = el('div', 'turn-user');
    if (turn.selections?.length) {
      const refs = el('div', 'changes');
      for (const selection of turn.selections) {
        const line = el('div', 'change');
        line.append(refNode(selection.file, selection.line, this.onGoTo));
        if (selection.instruction) line.append(el('span', '', selection.instruction));
        refs.append(line);
      }
      head.append(refs);
    }
    if (turn.message) head.append(el('div', '', turn.message));

    const badge = el('span', 'badge running', 'running');
    const headWrap = el('div', 'turn-actions');
    headWrap.append(badge);
    head.append(headWrap);

    const body = el('div', 'turn-body');
    const footer = el('div', 'turn-body');

    root.append(head, body, footer);
    this.els.messages.append(root);

    const card = { root, body, footer, badge };
    this.cards.set(turn.id, card);
    this._scroll();
    return card;
  }

  /** The chat view shows a summary: prose in full, everything else one line. */
  _appendToCard(turnId, entry) {
    const card = this.cards.get(turnId);
    if (!card) return;

    if (entry.kind === 'text') {
      if (entry.text.trim()) card.body.append(el('div', 'agent-text', entry.text));
    } else if (entry.kind === 'tool_use') {
      this._activity(card, entry.verb, entry.text, entry.isError ? 'bad' : '');
    } else if (entry.kind === 'build') {
      const tone = entry.isError ? 'bad' : entry.text.toLowerCase().includes('succeed') ? 'ok' : '';
      this._activity(card, 'build', entry.text, tone);
      if (entry.detail) card.body.append(el('pre', 'errors', entry.detail));
    } else if (entry.kind === 'notice' && entry.text) {
      this._activity(card, entry.isError ? 'error' : 'note', entry.text, entry.isError ? 'bad' : '');
    }
    // tool_result / thinking / system / result stay in the transcript view only.
    this._scroll();
  }

  _activity(card, verb, target, tone = '') {
    const line = el('div', `activity ${tone}`.trim());
    line.append(el('span', 'verb', verb));
    line.append(el('span', 'target', target || ''));
    card.body.append(line);
  }

  _renderCompletedTurn(turn) {
    const card = this._cardFor(turn);
    if (turn.agentText) card.body.append(el('div', 'agent-text', turn.agentText));
    this._finishCard(turn);
  }

  _finishCard(turn) {
    const card = this.cards.get(turn.id) || this._cardFor(turn);
    card.badge.className = `badge ${turn.status}`;
    card.badge.textContent = turn.status;

    card.footer.replaceChildren();

    if (turn.error) card.footer.append(el('pre', 'errors', turn.error));

    if (turn.changes?.length) {
      const list = el('div', 'changes');
      for (const change of turn.changes) {
        const row = el('div', 'change');
        const firstHunk = (turn.hunks || []).find((h) => h.file === change.file);
        row.append(
          firstHunk
            ? refNode(change.file, firstHunk.newStart, this.onGoTo, change.file)
            : el('span', 'file', change.file)
        );
        row.append(el('span', 'plus', `+${change.added}`));
        row.append(el('span', 'minus', `−${change.removed}`));
        list.append(row);
      }
      card.footer.append(list);

      const actions = el('div', 'turn-actions');
      const diffButton = el('button', 'ghost', 'Show diff');
      let diffNode = null;
      diffButton.addEventListener('click', async () => {
        if (diffNode) {
          diffNode.remove();
          diffNode = null;
          diffButton.textContent = 'Show diff';
          return;
        }
        try {
          const full = await getJSON(`/api/turns/${turn.id}`);
          diffNode = renderDiff(full.changes);
          card.footer.append(diffNode);
          diffButton.textContent = 'Hide diff';
        } catch (err) {
          showToast(err.message || String(err), { type: 'error' });
        }
      });
      actions.append(diffButton);

      if (!turn.reverted) {
        const revert = el('button', 'ghost', 'Revert');
        revert.addEventListener('click', async () => {
          revert.disabled = true;
          try {
            await postJSON(`/api/turns/${turn.id}/revert`, {});
            showToast('Reverted.');
          } catch (err) {
            revert.disabled = false;
            showToast(err.message || String(err), { type: 'error' });
          }
        });
        actions.append(revert);
      }

      if (turn.costUsd) {
        actions.append(el('span', 'hint', `$${turn.costUsd.toFixed(4)}`));
      }
      card.footer.append(actions);
    } else if (turn.costUsd) {
      const actions = el('div', 'turn-actions');
      actions.append(el('span', 'hint', `$${turn.costUsd.toFixed(4)}`));
      card.footer.append(actions);
    }

    this._scroll();
  }

  _scroll() {
    if (this.view === 'chat') {
      this.els.messages.scrollTop = this.els.messages.scrollHeight;
    }
  }
}

function renderDiff(changes) {
  const pre = el('pre', 'diff');
  for (const change of changes) {
    for (const line of (change.diff || '').split('\n')) {
      let cls = '';
      if (line.startsWith('+') && !line.startsWith('+++')) cls = 'add';
      else if (line.startsWith('-') && !line.startsWith('---')) cls = 'del';
      else if (line.startsWith('@@')) cls = 'hunk';
      pre.append(el('span', cls, `${line}\n`));
    }
  }
  return pre;
}
