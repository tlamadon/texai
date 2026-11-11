// Comments, written and answered on the page.
//
// Two boxes, one shell. Selecting a passage offers a comment button beside the
// ✦; it opens a box to write a comment, which goes into the source as a
// `%@comment` on the line above the sentence (see comments.py). Clicking a note
// in the margin opens its thread: the note and its replies, who wrote each, and
// what can be done — reply, resolve or reopen, delete, or open it in the source.
//
// Every action is the reader's own edit of the source, saved and rebuilt like
// one; the page reloads with the margin up to date, so the box just closes.

import { getJSON, postJSON } from './api.js';
import { showToast } from './toast.js';

const WIDTH = 320; // px
const MAX_QUOTE = 90;

const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};

const trim = (text, limit) => {
  const flat = String(text || '').replace(/\s+/g, ' ').trim();
  return flat.length > limit ? `${flat.slice(0, limit - 1)}…` : flat;
};

const autosize = (area) => {
  area.style.height = 'auto';
  area.style.height = `${Math.min(area.scrollHeight, 200)}px`;
};

export class CommentBox {
  constructor({ viewer }) {
    this.viewer = viewer;
    this.container = viewer.container;
    this.box = null; // {node, place(), highlights}
    this.author = '';
    this.onEdit = null; // (file, line): open the source there
    getJSON('/api/info')
      .then((info) => {
        this.author = info.author || '';
      })
      .catch(() => {});

    this._outside = (event) => {
      if (this.box && !this.box.node.contains(event.target) && !event.target.closest?.('.note-card, .comment-button')) {
        // A box with words typed into it closes only when asked to.
        const typed = this.box.node.querySelector('textarea')?.value.trim();
        if (!typed) this.close();
      }
    };
    this._escape = (event) => {
      if (event.key === 'Escape' && this.box) {
        event.preventDefault();
        this.close();
      }
    };
  }

  get isOpen() {
    return !!this.box;
  }

  close() {
    if (!this.box) return;
    this.box.node.remove();
    for (const node of this.box.highlights || []) node.remove();
    this.box = null;
    document.removeEventListener('mousedown', this._outside, true);
    document.removeEventListener('keydown', this._escape);
  }

  /** Keep the open box with what it hangs off, through zooms and re-renders. */
  place() {
    this.box?.place();
  }

  _shell(title) {
    this.close();
    const node = el('div', 'comment-box');
    node.setAttribute('role', 'dialog');
    const head = el('div', 'comment-head');
    const label = el('span', 'comment-title', title);
    const close = el('button', 'ghost icon', '×');
    close.type = 'button';
    close.title = 'Close (Esc)';
    close.addEventListener('click', () => this.close());
    head.append(label, close);
    node.append(head);
    // The page underneath reads clicks as jumps, and double-clicks as edits.
    for (const type of ['click', 'mousedown', 'dblclick', 'mouseup']) {
      node.addEventListener(type, (event) => event.stopPropagation());
    }
    this.container.append(node);
    document.addEventListener('mousedown', this._outside, true);
    document.addEventListener('keydown', this._escape);
    return { node, label };
  }

  /** Put the box under a rectangle in the scroll area's coordinates. */
  _put(node, rect) {
    const room = this.container.scrollWidth;
    const width = Math.min(WIDTH, room - 16);
    node.style.width = `${width}px`;
    node.style.left = `${Math.max(8, Math.min(rect.left + rect.width - width, room - width - 8))}px`;
    node.style.top = `${rect.top + rect.height + 6}px`;
  }

  _composer(placeholder, submitLabel, onSubmit) {
    const wrap = el('div', 'comment-compose');
    const input = el('textarea', 'comment-input');
    input.rows = 2;
    input.placeholder = placeholder;
    const bar = el('div', 'comment-bar');
    const status = el('span', 'comment-status');
    const submit = el('button', 'primary', submitLabel);
    submit.type = 'button';
    bar.append(status, submit);
    wrap.append(input, bar);

    const go = async () => {
      const text = input.value.trim();
      if (!text || submit.disabled) return;
      submit.disabled = true;
      input.disabled = true;
      status.className = 'comment-status busy';
      status.textContent = 'Saving…';
      try {
        await onSubmit(text);
      } catch (err) {
        submit.disabled = false;
        input.disabled = false;
        status.className = 'comment-status err';
        status.textContent = err.message || String(err);
      }
    };
    input.addEventListener('input', () => autosize(input));
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        go();
      }
    });
    submit.addEventListener('click', go);
    return { wrap, input, status };
  }

  /* ---------------- a new comment on a passage ---------------- */

  /**
   * `passage` is what the selection captured (see ask.js): its text, a point
   * on the page to ask SyncTeX about, and its line boxes in PDF points.
   */
  async compose(passage) {
    const { node, label } = this._shell('Comment');
    const quote = el('div', 'comment-quote', `“${trim(passage.text, MAX_QUOTE)}”`);
    let where = null;
    const composer = this._composer('Add a comment…', 'Comment', async (text) => {
      if (!where) throw new Error('Could not find this passage in the source.');
      await postJSON('/api/comments', { action: 'add', file: where.file, line: where.line, text });
      this.close();
      showToast(`Comment added at ${where.file}:${where.line}.`);
    });
    node.append(quote, composer.wrap);

    const highlights = passage.rects.map(() => {
      const mark = el('div', 'ask-highlight');
      this.container.append(mark);
      return mark;
    });
    const spot = (rect) => {
      const entry = this.viewer.pageEntry(rect.page);
      if (!entry?.viewport) return null;
      return this.viewer.pageRectToContainer(entry, this.viewer.pdfRectToPageRect(entry, rect));
    };
    const place = () => {
      passage.rects.forEach((rect, i) => {
        const r = spot(rect);
        const mark = highlights[i];
        mark.hidden = !r;
        if (!r) return;
        Object.assign(mark.style, {
          left: `${r.left}px`,
          top: `${r.top}px`,
          width: `${r.width}px`,
          height: `${r.height}px`,
        });
      });
      const last = spot(passage.rects[passage.rects.length - 1]);
      if (last) this._put(node, last);
    };
    this.box = { node, place, highlights };
    place();
    composer.input.focus();
    window.getSelection()?.removeAllRanges();

    try {
      // A block of a slide knows where it comes from; anything else is asked.
      where =
        passage.source ??
        (await postJSON('/api/resolve', {
          page: passage.page,
          x: Number(passage.point.x.toFixed(3)),
          y: Number(passage.point.y.toFixed(3)),
          selectedText: passage.text || null,
          word: passage.word?.word ?? null,
          contextBefore: passage.word?.before ?? null,
          contextAfter: passage.word?.after ?? null,
        }));
      if (this.box?.node === node) {
        label.textContent = `Comment on ${where.file}:${where.line}`;
        if (this.author) label.textContent += ` as ${this.author}`;
      }
    } catch (err) {
      if (this.box?.node !== node) return;
      composer.status.className = 'comment-status err';
      composer.status.textContent = err.message || String(err);
    }
  }

  /* ---------------- a thread in the margin ---------------- */

  /**
   * `item` is a note from the margin; `items` all of them, to gather its
   * thread from; `anchor()` finds its card on the page, as it is drawn now.
   */
  thread(item, items, anchor) {
    const root = threadRoot(item, items);
    const replies = threadReplies(root, items);
    const name = root.kind === 'comment' ? 'Comment' : root.kind[0].toUpperCase() + root.kind.slice(1);
    const { node } = this._shell(`${name} · ${root.file}:${root.line}`);

    const act = async (action, message, extra = {}) => {
      await postJSON('/api/comments', {
        action,
        file: message.file,
        line: message.line,
        kind: message.kind,
        said: message.text,
        // Kept in annotations.jsonl: named by its record, not its words.
        ...(message.record ? { record: message.record, target: message.cid } : {}),
        ...extra,
      });
      this.close();
    };

    const list = el('div', 'comment-thread');
    for (const message of [root, ...replies]) {
      list.append(this._message(message, message === root, act));
    }
    node.append(list);

    const composer = this._composer(
      root.status === 'done' ? 'Reply (it stays resolved)…' : 'Reply…',
      'Reply',
      async (text) => {
        await act('reply', root, { text });
        showToast('Reply added.');
      }
    );
    node.append(composer.wrap);

    const tools = el('div', 'comment-tools');
    const tool = (text, title, onClick) => {
      const button = el('button', 'ghost', text);
      button.type = 'button';
      button.title = title;
      button.addEventListener('click', onClick);
      tools.append(button);
      return button;
    };
    const fail = (err) => showToast(err.message || String(err), { type: 'error' });
    if (root.status === 'done') {
      tool('Reopen', 'Mark it as open again', () => act('reopen', root).catch(fail));
    } else {
      tool('Resolve', 'Mark it as settled: it stays in the source, faded', () => act('resolve', root).catch(fail));
    }
    const remove = tool(
      replies.length ? 'Delete thread' : 'Delete',
      'Remove it from the source',
      () => {
        // Twice, so a slip of the mouse does not lose a conversation.
        if (!remove.classList.contains('confirm')) {
          remove.classList.add('confirm');
          remove.textContent = 'Click again to delete';
          return;
        }
        act('delete', root)
          .then(() => showToast(replies.length ? 'Thread deleted.' : 'Deleted.'))
          .catch(fail);
      }
    );
    tools.append(el('div', 'spacer'));
    tool('Source', `Open ${root.file}:${root.line} in the editor`, () => {
      this.close();
      this.onEdit?.(root.file, root.line);
    });
    node.append(tools);

    const place = () => {
      const card = anchor();
      if (!card) return;
      const rect = this.viewer.pageRectToContainer(card.entry, {
        left: card.node.offsetLeft,
        top: card.node.offsetTop,
        width: card.node.offsetWidth,
        height: card.node.offsetHeight,
      });
      this._put(node, rect);
    };
    this.box = { node, place, highlights: [] };
    place();
  }

  _message(message, isRoot, act) {
    const row = el('div', `comment-message${message.status === 'done' ? ' done' : ''}`);
    const head = el('div', 'comment-meta');
    head.append(el('span', 'comment-by', message.by || 'unsigned'));
    if (message.kind !== 'comment') head.append(el('span', 'comment-kind', message.kind));
    if (message.status) head.append(el('span', 'comment-state', message.status));
    if (!isRoot) {
      const remove = el('button', 'ghost icon comment-remove', '×');
      remove.type = 'button';
      remove.title = 'Delete this reply';
      remove.addEventListener('click', () => {
        act('delete', message).catch((err) => showToast(err.message || String(err), { type: 'error' }));
      });
      head.append(remove);
    }
    row.append(head, el('div', 'comment-text', message.text));
    return row;
  }
}

/** The top of the thread a note is in: up its `re:` chain, as far as it goes. */
export function threadRoot(item, items) {
  let root = item;
  const seen = new Set();
  while (root.re && !seen.has(root.re)) {
    seen.add(root.re);
    const parent = items.find((other) => other.file === root.file && other.id === root.re);
    if (!parent) break;
    root = parent;
  }
  return root;
}

/** Every reply under a note, at any depth, in source order. */
export function threadReplies(root, items) {
  if (!root.id) return [];
  const ids = new Set([root.id]);
  const replies = [];
  let grew = true;
  while (grew) {
    grew = false;
    for (const other of items) {
      if (other.file === root.file && other.re && ids.has(other.re) && !replies.includes(other)) {
        replies.push(other);
        if (other.id) ids.add(other.id);
        grew = true;
      }
    }
  }
  return replies.sort((a, b) => a.line - b.line);
}
