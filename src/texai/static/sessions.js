// Which agent conversation the panel is talking to, and the others it could be.
//
// The header button names the current conversation; its menu lists every one
// saved for this project — those texai ran, and those started elsewhere, in a
// Claude Code or Codex terminal — and switches to any of them, or to a new one.
// The switch takes effect with the next turn: the agent starts lazily, as it
// always has. Pending changes stay as they are, because they are measured from
// the files and not from what any conversation remembers doing.
//
// Turns run outside texai have no snapshots, so a conversation taken over from
// a terminal can be read and carried on, but its earlier work not reverted.

import { getJSON, postJSON } from './api.js';
import { showToast } from './toast.js';

const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};

const shortId = (id) => (id ? id.slice(0, 8) : '');

/** "3 min ago", "yesterday", "12 Mar" — when a conversation was last used. */
export function ago(ms, now = Date.now()) {
  if (!ms) return '';
  const minutes = Math.round((now - ms) / 60000);
  if (minutes < 1) return 'just now';
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  const days = Math.round(hours / 24);
  if (days === 1) return 'yesterday';
  if (days < 7) return `${days} days ago`;
  return new Date(ms).toLocaleDateString(undefined, { day: 'numeric', month: 'short' });
}

/** One line of facts about a conversation: turns, when, which. */
export function sessionMeta(session) {
  const bits = [];
  if (session.turns) bits.push(`${session.turns} turn${session.turns === 1 ? '' : 's'}`);
  const when = ago(session.updatedAt);
  if (when) bits.push(when);
  bits.push(shortId(session.id));
  return bits.join(' · ');
}

export class SessionMenu {
  constructor() {
    this.els = {
      button: document.getElementById('session-button'),
      label: document.getElementById('session-label'),
      pop: document.getElementById('session-menu'),
    };
    this.state = null; // the last /api/sessions answer
    this.busy = false;
    this.onCopyResume = null; // copy `claude --resume <id>` for a terminal
    this._wire();
  }

  _wire() {
    const { button, pop } = this.els;
    button.addEventListener('click', () => this.toggle(pop.hidden));
    // Click-away and Escape, as the editor's menu closes.
    document.addEventListener('click', (event) => {
      if (pop.hidden || button.contains(event.target) || pop.contains(event.target)) return;
      this.toggle(false);
    });
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && !pop.hidden) this.toggle(false);
    });
  }

  /** The current conversation as the catalogue describes it, if it is in it. */
  get current() {
    const id = this.state?.current;
    return id ? this.state.sessions.find((s) => s.id === id) || { id } : null;
  }

  async refresh() {
    try {
      this.state = await getJSON('/api/sessions');
    } catch {
      return this.state;
    }
    this._renderLabel();
    if (!this.els.pop.hidden) this._renderMenu();
    return this.state;
  }

  /** The agent learned or changed its conversation; the list may be stale too. */
  noteSession(sessionId) {
    if (this.state) this.state.current = sessionId;
    this._renderLabel();
    this.refresh();
  }

  setBusy(busy) {
    this.busy = busy;
    if (!this.els.pop.hidden) this._renderMenu();
  }

  toggle(open) {
    const { button, pop } = this.els;
    pop.hidden = !open;
    button.setAttribute('aria-expanded', String(open));
    button.classList.toggle('on', open);
    if (!open) return;
    // Draw what is known now, then again once the list is fresh: Codex has to
    // start its app server to answer.
    this._renderMenu(this.state ? null : 'Looking for saved conversations…');
    this.refresh();
  }

  async switchTo(sessionId) {
    this.toggle(false);
    try {
      await postJSON('/api/sessions/switch', { sessionId });
    } catch (err) {
      showToast(err.message || String(err), { type: 'error' });
      return;
    }
    // The server announces the switch on the event stream; the chat draws it.
    this.noteSession(sessionId);
  }

  _renderLabel() {
    const { button, label } = this.els;
    const current = this.current;
    const name = this.state?.name || 'the agent';
    if (!current) {
      label.textContent = 'New conversation';
      button.title = `The next message starts a new conversation with ${name}. Click to resume or switch.`;
      return;
    }
    label.textContent = current.title || `Conversation ${shortId(current.id)}`;
    button.title = `Talking to ${name} in ${current.id}. Click to switch, resume or start anew.`;
  }

  _renderMenu(message) {
    const pop = this.els.pop;
    pop.replaceChildren();
    const state = this.state;
    const name = state?.name || 'the agent';
    const current = this.current;

    const head = el('div', 'session-current');
    head.append(el('div', 'session-kicker', 'Talking to'));
    if (current) {
      head.append(el('div', 'session-title', current.title || 'This conversation'));
      head.append(el('div', 'menu-note', sessionMeta(current)));
    } else {
      head.append(el('div', 'session-title', 'A new conversation'));
      head.append(el('div', 'menu-note', 'It starts with your next message.'));
    }
    pop.append(head);

    if (current) {
      pop.append(
        this._item('Start a new conversation', `${name} forgets this one; the review stays as it is.`, () =>
          this.switchTo(null),
        ),
      );
    }
    if (state?.resumeCommand) {
      pop.append(
        this._item('Copy terminal command', state.resumeCommand, () => {
          this.toggle(false);
          this.onCopyResume?.(state.resumeCommand);
        }),
      );
    }

    if (message) {
      pop.append(el('div', 'menu-note session-empty', message));
      return;
    }
    const others = (state?.sessions || []).filter((s) => !s.current);
    const mine = others.filter((s) => s.texai);
    const elsewhere = others.filter((s) => !s.texai);
    if (!others.length) {
      pop.append(el('div', 'menu-note session-empty', 'No other saved conversations for this project.'));
      return;
    }
    if (this.busy) {
      pop.append(el('div', 'menu-note session-empty', 'A turn is running — switch once it is done.'));
    }
    this._section(pop, 'Earlier in texai', mine);
    this._section(
      pop,
      `From ${name === 'Codex' ? 'Codex' : 'Claude Code'} in a terminal`,
      elsewhere,
      'Its earlier edits can be read here, but not reverted.',
    );
  }

  _section(pop, title, sessions, note) {
    if (!sessions.length) return;
    const head = el('div', 'session-section', title);
    if (note) head.title = note;
    pop.append(head);
    const list = el('div', 'session-list');
    for (const session of sessions) {
      const item = this._item(session.title, sessionMeta(session), () => this.switchTo(session.id));
      item.disabled = this.busy;
      item.title = session.id;
      list.append(item);
    }
    pop.append(list);
  }

  _item(label, note, action) {
    const item = el('button', 'menu-item');
    item.type = 'button';
    item.setAttribute('role', 'menuitem');
    item.append(el('span', 'menu-label', label));
    if (note) item.append(el('span', 'menu-note', note));
    item.addEventListener('click', action);
    return item;
  }
}

/** What was said in a saved conversation, folded away under one line. */
export async function pastConversation(sessionId) {
  const { messages } = await getJSON(`/api/sessions/${encodeURIComponent(sessionId)}/messages`);
  if (!messages?.length) return null;
  const box = el('details', 'session-past');
  const replies = messages.filter((m) => m.role === 'user').length;
  box.append(
    el('summary', '', `Earlier in this conversation — ${replies} request${replies === 1 ? '' : 's'}`),
  );
  for (const message of messages) {
    const row = el('div', `session-msg ${message.role}`);
    row.append(el('div', 'session-role', message.role === 'user' ? 'You' : 'Agent'));
    row.append(el('div', 'session-text', message.text));
    box.append(row);
  }
  return box;
}
