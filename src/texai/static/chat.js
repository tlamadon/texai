// The chat panel: selection chips, the composer, and the live agent stream.
//
// Two views over one stream of transcript entries. "Chat" renders a friendly
// summary per turn; "Transcript" renders the same entries verbatim, the way a
// terminal session would. Building both from one stream means the transcript
// cannot show something the chat quietly dropped.

import { getJSON, postJSON } from './api.js';
import { markChosen, renderOffer } from './options.js';
import { ProgressBar, progressLabels } from './progress.js';
import { SessionMenu, pastConversation } from './sessions.js';
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
      tabSource: document.getElementById('tab-source'),
      editorPane: document.getElementById('editor-pane'),
    };
    this.sessions = new SessionMenu();
    this.sessions.onCopyResume = (command) => this.copyResumeCommand(command);

    this.chips = [];
    this.cards = new Map(); // turnId -> {root, body, badge, footer}
    this.agent = { available: false, reason: null };
    this.busy = false;
    this.lastEventId = 0;
    this.view = 'chat';
    // The conversation the last drawn turn ran in, so a change shows as a rule.
    this.lastSession = undefined;
    this.lastTranscriptTurn = null;
    this.onTurnFinished = null;
    this.onAgentEntry = null; // (turnId, entry) for each step of a running turn
    this.onBuild = null; // (event) when a turn's build starts or finishes
    this.onChangesUpdated = null;
    this.onGoTo = null;      // click a file:line reference
    this.onEdit = null;      // open a marked passage in the editor
    this.onNavigate = null;  // the agent moved the view itself
    this.onViewChange = null; // chat / transcript / source

    this._wire();
  }

  /* ---------------- wiring ---------------- */

  _wire() {
    this.els.send.addEventListener('click', () => this.send());
    this.els.clear.addEventListener('click', () => this.clearChips());
    this.els.interrupt.addEventListener('click', () => this.interrupt());
    this.els.tabChat.addEventListener('click', () => this.setView('chat'));
    this.els.tabTranscript.addEventListener('click', () => this.setView('transcript'));
    this.els.tabSource.addEventListener('click', () => this.setView('source'));
    this.els.input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        this.send();
      }
    });
  }

  setView(view) {
    this.view = view;
    this.els.messages.hidden = view !== 'chat';
    this.els.transcript.hidden = view !== 'transcript';
    this.els.editorPane.hidden = view !== 'source';
    this.els.tabChat.classList.toggle('active', view === 'chat');
    this.els.tabTranscript.classList.toggle('active', view === 'transcript');
    this.els.tabSource.classList.toggle('active', view === 'source');
    if (view === 'transcript') this._scrollTranscript();
    this.onViewChange?.(view);
  }

  async start() {
    try {
      const info = await getJSON('/api/info');
      this.agent = info.agent || { available: false, reason: 'unknown' };
      this._renderStatus();
    } catch {
      this._renderStatus();
    }
    await this.sessions.refresh();

    try {
      const history = await getJSON('/api/turns');
      for (const turn of history.turns) {
        this._noteTurnSession(turn);
        if (turn.status === 'running') {
          // Still going: drawn the way live events draw it, and the stream
          // carries on from where this snapshot stops.
          this._cardFor(turn);
          for (const entry of turn.transcript || []) {
            this._appendTranscript(turn.id, entry);
            this._appendToCard(turn.id, entry);
          }
          continue;
        }
        this._renderCompletedTurn(turn);
        for (const entry of turn.transcript || []) this._appendTranscript(turn.id, entry);
        this._appendTurnRule(turn);
      }
      // Everything up to here is already on screen. Streaming from the start
      // would replay it all: entries drawn twice, old jumps scrolling the page.
      this.lastEventId = history.lastEventId ?? this.lastEventId;
      this._setBusy(history.busy);
      this.onChangesUpdated?.();
    } catch {
      /* no history yet */
    }
    // Resumed at launch, or switched since the last turn: say so, and what
    // was said there before.
    const current = this.agent.sessionId || null;
    if (current !== (this.lastSession ?? null)) this._showSession(current);

    this._connect();
  }

  /* ---------------- which conversation ---------------- */

  /**
   * A turn's conversation, drawn as a rule when it is not the one before.
   * A new conversation learns its id during its first turn; that is not a
   * change worth a rule.
   */
  _noteTurnSession(turn) {
    const id = turn.sessionId;
    if (!id || id === this.lastSession) return;
    if (this.lastSession) this._sessionRule(id);
    this.lastSession = id;
  }

  _sessionRule(sessionId) {
    if (this.els.placeholder) this.els.placeholder.remove();
    const known = this.sessions.state?.sessions?.find((s) => s.id === sessionId);
    const label = sessionId
      ? `Now in: ${known?.title || `conversation ${sessionId.slice(0, 8)}`}`
      : 'New conversation — the agent starts fresh';
    const rule = el('div', 'session-rule', label);
    if (sessionId) rule.title = sessionId;
    this.els.messages.append(rule);
    this.els.transcript.append(
      el('span', 't-rule', `\n════ ${sessionId ? `conversation ${sessionId}` : 'new conversation'} ════\n`),
    );
    this.lastTranscriptTurn = null;
    this._scroll();
    return rule;
  }

  /** Switched (here, in another tab, or at launch): a rule, then the past. */
  async _showSession(sessionId) {
    this.lastSession = sessionId;
    this.agent = { ...this.agent, sessionId };
    await this.sessions.refresh();
    const rule = this._sessionRule(sessionId);
    if (!sessionId) return;
    try {
      const past = await pastConversation(sessionId);
      if (past) {
        rule.after(past);
        this._scroll();
      }
    } catch (err) {
      showToast(`Could not read the earlier conversation: ${err.message || err}`, { type: 'error' });
    }
  }

  /* ---------------- selection chips ---------------- */

  addSelection(selection) {
    const source = selection.source || {};
    const chip = {
      id: `c${Date.now()}${this.chips.length}`,
      file: source.file,
      line: source.line,
      column: source.column ?? 1,
      word: source.word ?? null,
      page: selection.page ?? null,
      selectedText: selection.selectedText ?? null,
      instruction: '',
    };
    if (!chip.file || !chip.line) return;

    // Re-clicking the same spot replaces rather than duplicates. Two words on
    // one line are two different spots now, so the column is part of identity.
    const existing = this.chips.findIndex(
      (c) => c.file === chip.file && c.line === chip.line && c.column === chip.column
    );
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
      // The column only earns its place when it points at a known word.
      const label = chip.word ? `${chip.file}:${chip.line}:${chip.column}` : `${chip.file}:${chip.line}`;
      const ref = el('span', 'chip-ref ref', label);
      ref.title = chip.word
        ? `Show this passage in the PDF — the word “${chip.word}”`
        : 'Show this passage in the PDF';
      ref.addEventListener('click', () => this.onGoTo?.(chip.file, chip.line));
      head.append(ref);
      head.append(
        el('span', 'chip-text', chip.selectedText ? `“${truncate(chip.selectedText, MAX_CHIP_TEXT)}”` : '')
      );
      // Marking a spot and then editing it by hand is the obvious next move,
      // so it gets its own button rather than only living on Alt-click.
      const edit = el('button', 'chip-edit', '\u270e');
      edit.title = chip.word
        ? `Open the source at “${chip.word}”`
        : `Open ${chip.file}:${chip.line} in the editor`;
      edit.addEventListener('click', () => this.onEdit?.(chip.file, chip.line, chip.column, chip.word));
      head.append(edit);

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
      word: chip.word ?? null,
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

  async copyResumeCommand(command = this.agent.resumeCommand) {
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
    this.sessions.setBusy(busy);
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
        this._cardFor(event.turn, Date.now());
        break;
      case 'agent_entry':
        this._appendTranscript(event.turnId, event.entry);
        this._appendToCard(event.turnId, event.entry);
        this.onAgentEntry?.(event.turnId, event.entry);
        break;
      case 'build_started':
      case 'build_finished':
        this._buildProgress(event);
        this.onBuild?.(event);
        break;
      case 'turn_finished':
        this._noteTurnSession(event.turn);
        this._finishCard(event.turn);
        this._appendTurnRule(event.turn);
        this._setBusy(false);
        this.onTurnFinished?.(event.turn);
        break;
      case 'agent_ready':
        this.agent = { ...this.agent, available: true, reason: null };
        this._renderStatus();
        break;
      case 'changes_updated':
        this.onChangesUpdated?.();
        this._refreshTurnStates();
        break;
      case 'navigate':
        this.onNavigate?.(event);
        break;
      case 'offer_picked': {
        // Picked on the page or here: either way, show which one went in.
        const node = this.cards.get(event.turnId)?.offers?.[event.offer];
        if (node) markChosen(node, event.option);
        break;
      }
      case 'agent_session':
        this.agent = {
          ...this.agent,
          sessionId: event.sessionId,
          resumeCommand: event.resumeCommand,
        };
        this.sessions.noteSession(event.sessionId);
        break;
      case 'session_switched':
        this._showSession(event.sessionId ?? null);
        break;
      default:
        break;
    }
  }

  /** Re-badge every turn from the server's view of the review. */
  async _refreshTurnStates() {
    try {
      const { turns } = await getJSON('/api/turns');
      for (const turn of turns) {
        const card = this.cards.get(turn.id);
        if (card) this._badge(card, turn);
      }
    } catch {
      /* the badges will catch up on the next event */
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

  /**
   * `startedAt` is when a live turn's start reached the page; a turn found
   * running on load falls back to its creation time, to the second.
   */
  _cardFor(turn, startedAt = Date.parse(turn.createdAt) || Date.now()) {
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
    if (turn.status === 'running' && turn.timing?.estimate) {
      card.progress = new ProgressBar(progressLabels(turn));
      root.append(card.progress.el);
      card.progress.start(turn.timing.estimate, startedAt);
    }
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

  /**
   * The badge says what became of the turn's edits, not just that they landed.
   * A turn stays "applied" until its changes are reviewed, then reads
   * accepted, rejected, or mixed.
   */
  _badge(card, turn) {
    const reviewed = turn.status === 'applied' && turn.review && turn.review !== 'pending';
    const label = reviewed ? turn.review : turn.status;
    card.badge.className = `badge ${label}`;
    card.badge.textContent = label;
    card.badge.title = reviewed ? `applied, then ${turn.review}` : '';
  }

  /** A build began or ended: move the running card's bar along. */
  _buildProgress(event) {
    const progress = this.cards.get(event.turnId)?.progress;
    if (!progress) return;
    if (event.type === 'build_started') progress.phase('compiling');
    else if (!event.ok) progress.retry();
  }

  _finishCard(turn) {
    const card = this.cards.get(turn.id) || this._cardFor(turn);
    this._badge(card, turn);
    if (card.progress) {
      card.progress.stop();
      card.progress.el.remove();
      card.progress = null;
    }

    card.footer.replaceChildren();

    if (turn.error) card.footer.append(el('pre', 'errors', turn.error));

    // Versions the agent offered instead of editing: pickable here too, so
    // closing the box on the page does not lose them.
    card.offers = (turn.offers || []).map((offer, k) => {
      const node = renderOffer(offer, {
        compact: true,
        // Marked as picked once it has landed (offer_picked), not on the click:
        // a pick that does not build is rolled back.
        onPick: async (i) => {
          try {
            await postJSON(`/api/turns/${turn.id}/pick`, { offer: k, option: i });
          } catch (err) {
            showToast(err.message || String(err), { type: 'error' });
          }
        },
      });
      card.footer.append(node);
      return node;
    });

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

