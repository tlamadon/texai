// Versions of a passage the agent offered instead of editing it, laid out for
// picking one.
//
// Each version shows the source it would put in place (raw LaTeX for now),
// with the words it changes marked: versions of one sentence often differ by a
// few words, and those are hard to spot side by side. The pick itself is the
// caller's — the box on the page and the chat card both send it to
// /api/turns/<id>/pick, where texai puts the version in place.
//
// Or none of them, yet: "Keep as proposal" writes the versions into the source
// as a %@propose choice (/api/turns/<id>/keep), to decide later from the margin.
//
// texai's annotations and LaTeX comments are folded to a chip each, as in the
// inline editor and by the same two switches, so a paragraph dense with %@src
// lines shows its prose; a chip is marked when the version changes what it hides.

import { HIDDEN, commentSpans, foldShown, unrenderedSpans } from './folds.js';

const MAX_DIFF_CELLS = 250_000; // past this, show the text unmarked

// A folded stretch stands in the texts being compared as one token, between
// two private-use characters, which LaTeX source does not contain.
const FOLD_OPEN = '\uE000';
const FOLD_CLOSE = '\uE001';
const FOLD_TOKEN = /^\uE000(\d+)\uE001$/;
const TOKENS = /\uE000\d+\uE001|\s+|[^\s\uE000]+/g;

const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};

// How the versions are laid out, and how much of each is shown. Remembered,
// apart for the box on the page and the narrow chat panel.
const LAYOUTS = [
  { key: 'side', label: 'side by side', title: 'The versions next to each other' },
  { key: 'stack', label: 'stacked', title: 'The versions one above the other' },
  { key: 'one', label: 'one at a time', title: 'One version, with arrows to the others' },
];
const SCOPES = [
  { key: 'changed', label: 'changed part', title: 'Only the sentences that differ between the versions' },
  { key: 'whole', label: 'whole passage', title: 'The whole passage each version would put in place' },
];
const SCOPE_KEY = 'texai:offer-scope';
const layoutKey = (compact) => (compact ? 'texai:offer-layout-chat' : 'texai:offer-layout');

/** The layout versions are shown in: 'side', 'stack' or 'one'. */
export function offerLayout(compact = false) {
  const stored = localStorage.getItem(layoutKey(compact));
  if (LAYOUTS.some((l) => l.key === stored)) return stored;
  return compact ? 'stack' : 'side';
}

const CLOSERS = `["'”’)}\\]]*`;
const SENTENCE_END = new RegExp(`[.!?]${CLOSERS}\\s+|\\n[ \\t]*\\n\\s*`, 'g');
const ENDS_SENTENCE = new RegExp(`[.!?]${CLOSERS}$`);
const NEXT_END = new RegExp(`[.!?]${CLOSERS}(?=\\s|$)|\\n[ \\t]*\\n`, 'g');

/**
 * The part of a passage its versions disagree on, widened to whole sentences:
 * how many characters every text shares at the start and at the end that can
 * go unshown. A request about a few words is answered with the whole
 * paragraph, since that is what gets replaced; reading three paragraphs to
 * find the one sentence that differs is not the point of offering them.
 */
export function changedSpan(texts) {
  const [first] = texts;
  const shortest = Math.min(...texts.map((t) => t.length));
  let head = 0;
  while (head < shortest && texts.every((t) => t[head] === first[head])) head++;
  let tail = 0;
  while (
    tail < shortest - head &&
    texts.every((t) => t[t.length - 1 - tail] === first[first.length - 1 - tail])
  ) {
    tail++;
  }
  // A full stop in a comment ends no sentence: the %@src lines above a sentence
  // go with it, and the cut never lands inside one.
  const comments = commentSpans(first);
  const inComment = (at) => comments.some(([from, to]) => at >= from && at < to);
  // Back to the start of the sentence the first difference is in.
  let start = 0;
  for (const found of first.slice(0, head).matchAll(SENTENCE_END)) {
    if (!inComment(found.index)) start = found.index + found[0].length;
  }
  // On to the end of the sentence the last difference is in.
  const base = first.length - tail;
  const shared = first.slice(base);
  let keep = shared.length;
  if (
    /^\s/.test(shared) &&
    !inComment(base - 1) &&
    texts.every((t) => ENDS_SENTENCE.test(t.slice(0, t.length - tail)))
  ) {
    keep = 0; // the difference ends its sentence already
  } else {
    for (const end of shared.matchAll(NEXT_END)) {
      if (inComment(base + end.index)) continue;
      keep = end.index + (end[0].startsWith('\n') ? 0 : end[0].length);
      break;
    }
  }
  return { start, drop: tail - keep };
}

function radios(choices, current, onChoose) {
  const group = el('div', 'offer-switch');
  group.setAttribute('role', 'radiogroup');
  for (const { key, label, title } of choices) {
    const button = el('button', '', label);
    button.type = 'button';
    button.title = title;
    button.dataset.key = key;
    button.setAttribute('role', 'radio');
    button.addEventListener('click', (event) => {
      event.stopPropagation();
      onChoose(key);
    });
    group.append(button);
  }
  group.choose = (key) => {
    for (const button of group.children) {
      button.setAttribute('aria-checked', String(button.dataset.key === key));
    }
  };
  group.choose(current);
  return group;
}

/**
 * One offer as cards, one per version. `onPick(i)` runs when version `i` is
 * chosen; `onKeep()`, when given, adds a button to keep them all as a proposal
 * instead, and `onAgain()` one to ask for fresh versions. `compact` is the
 * narrow chat panel, where they start stacked. `onLayout()` hears that the
 * layout changed, for a box that sizes itself to it.
 */
export function renderOffer(offer, { onPick, onKeep, onAgain, onLayout, compact = false } = {}) {
  const root = el('div', compact ? 'offer compact' : 'offer');
  if (offer.note) root.append(el('div', 'offer-note', offer.note));

  const span = changedSpan([offer.original, ...offer.options.map((o) => o.text)]);
  const trims = span.start > 0 || span.drop > 0;
  let layout = offerLayout(compact);
  let scope = localStorage.getItem(SCOPE_KEY) === 'whole' ? 'whole' : 'changed';
  let shown = Math.max(0, offer.picked ?? 0);

  const view = el('div', 'offer-view');
  const layouts = radios(LAYOUTS, layout, (key) => {
    layout = key;
    localStorage.setItem(layoutKey(compact), key);
    draw();
    onLayout?.(key);
  });
  const scopes = radios(SCOPES, scope, (key) => {
    scope = key;
    localStorage.setItem(SCOPE_KEY, key);
    draw();
    onLayout?.(layout);
  });
  scopes.hidden = !trims; // nothing to leave out: the versions differ throughout
  const step = el('div', 'offer-step');
  const earlier = el('button', 'ghost', '‹');
  const later = el('button', 'ghost', '›');
  const count = el('span', 'offer-count');
  earlier.type = later.type = 'button';
  earlier.title = 'The version before';
  later.title = 'The next version';
  const move = (by) => (event) => {
    event.stopPropagation();
    shown = (shown + by + offer.options.length) % offer.options.length;
    draw();
  };
  earlier.addEventListener('click', move(-1));
  later.addEventListener('click', move(1));
  step.append(earlier, count, later);
  // Show or hide what is folded: the inline editor's switches, and its setting.
  const folders = HIDDEN.map(({ kind, key, label, title }) => {
    const button = el('button', 'offer-fold', label);
    button.type = 'button';
    button.title = `Show or hide ${title}`;
    button.dataset.kind = kind;
    button.addEventListener('click', (event) => {
      event.stopPropagation();
      localStorage.setItem(key, foldShown(kind) ? '0' : '1');
      draw();
      onLayout?.(layout);
    });
    return button;
  });
  view.append(layouts, scopes, ...folders, step);
  root.append(view);

  const row = el('div', 'offer-options');
  const texts = [];
  offer.options.forEach((option, i) => {
    const card = el('div', 'offer-option');
    card.tabIndex = 0;
    card.setAttribute('role', 'button');
    card.title = `Use this version (${i + 1})`;
    card.classList.toggle('picked', offer.picked === i);
    const head = el('div', 'offer-label');
    head.append(el('span', 'offer-key', String(i + 1)), el('span', '', option.label));
    const text = el('div', 'offer-text');
    texts.push(text);
    card.append(head, text);
    card.addEventListener('click', () => onPick?.(i));
    card.addEventListener('keydown', (event) => {
      if (event.key !== 'Enter' && event.key !== ' ') return;
      event.preventDefault();
      onPick?.(i);
    });
    row.append(card);
  });
  root.append(row);

  function draw() {
    root.dataset.layout = layout;
    layouts.choose(layout);
    scopes.choose(scope);
    step.hidden = layout !== 'one';
    count.textContent = `${shown + 1} / ${offer.options.length}`;
    const cut = trims && scope === 'changed';
    const part = (text) => (cut ? text.slice(span.start, text.length - span.drop) : text);
    const folds = foldTable();
    const before = folds.hide(part(offer.original));
    offer.options.forEach((option, i) => {
      row.children[i].classList.toggle('current', i === shown);
      const nodes = marked(before, folds.hide(part(option.text)), folds);
      if (cut && span.start > 0) nodes.unshift(el('span', 'offer-elided', '… '));
      if (cut && span.drop > 0) nodes.push(el('span', 'offer-elided', ' …'));
      texts[i].replaceChildren(...nodes);
    });
    for (const button of folders) {
      button.hidden = !folds.kinds.has(button.dataset.kind);
      button.setAttribute('aria-pressed', String(foldShown(button.dataset.kind)));
    }
  }
  draw();

  if (onKeep) {
    const bar = el('div', 'offer-keep');
    const button = el('button', 'ghost', 'Keep as proposal');
    button.type = 'button';
    button.title =
      'Write these versions into the source as %@propose lines, and decide later from the margin';
    button.addEventListener('click', () => onKeep());
    bar.append(button);
    if (onAgain) {
      const again = el('button', 'ghost', 'Fresh versions');
      again.type = 'button';
      again.title = 'None of these: ask for new versions, different from the ones here';
      again.addEventListener('click', () => onAgain());
      bar.append(again);
    }
    bar.append(el('span', 'offer-kept'));
    root.append(bar);
    markKept(root, offer.id || '', offer.picked != null);
  }
  return root;
}

/** Show a rendered offer as kept in the source as proposal `id`, or as not. */
export function markKept(node, id, picked = false) {
  const button = node.querySelector('.offer-keep button'); // the first: Keep as proposal
  const note = node.querySelector('.offer-kept');
  if (!button || !note) return;
  button.hidden = !!id;
  button.disabled = picked;
  note.textContent = id ? `Kept in the source as proposal ${id}` : '';
}

/** Show version `i` of a rendered offer as the one chosen, the rest set aside. */
export function markChosen(node, i) {
  node.querySelectorAll('.offer-option').forEach((card, j) => {
    card.classList.toggle('chosen', i === j);
    card.classList.toggle('picked', i === j);
    // One at a time: the chosen one is the one to be looking at.
    if (i >= 0) card.classList.toggle('current', i === j);
  });
}

/**
 * What one drawing of an offer folds away. `hide(text)` puts a placeholder
 * token where each stretch that is not shown sits; stretches with the same text
 * share a token, in the original and the versions alike, so the comparison
 * sees an unchanged comment as an unchanged word. `kinds` is what was found,
 * folded or not, for the switches.
 */
function foldTable() {
  const ids = new Map();
  const stretches = [];
  const kinds = new Set();
  return {
    kinds,
    hide(text) {
      let out = '';
      let at = 0;
      for (const { kind, from, to } of unrenderedSpans(text)) {
        kinds.add(kind);
        if (foldShown(kind)) continue;
        const stretch = text.slice(from, to);
        const key = `${kind}:${stretch}`;
        if (!ids.has(key)) {
          ids.set(key, stretches.length);
          stretches.push({ kind, text: stretch });
        }
        out += `${text.slice(at, from)}${FOLD_OPEN}${ids.get(key)}${FOLD_CLOSE}`;
        at = to;
      }
      return out + text.slice(at);
    },
    /** The stretch a token stands for, or null for a word. */
    stretch(token) {
      const found = FOLD_TOKEN.exec(token);
      return found ? stretches[Number(found[1])] : null;
    },
  };
}

/** A folded stretch as a chip, its text in the title. A click shows it, and picks nothing. */
function foldChip({ kind, text }, changed) {
  const { label } = HIDDEN.find((h) => h.kind === kind);
  const chip = el('span', `offer-folded ${kind}${changed ? ' changed' : ''}`, label);
  chip.title = `${text.trim()}\n(${changed ? 'changed in this version; ' : ''}click to show)`;
  chip.addEventListener('click', (event) => {
    event.stopPropagation();
    chip.replaceWith(el('span', changed ? 'w-changed' : 'w-same', text));
  });
  return chip;
}

/**
 * `after` as spans, as a change's card on the page shows it: the words not in
 * `before` marked, the rest dimmed. A word-level longest common subsequence:
 * whitespace never counts as a change, and the space between two changed
 * words joins them into one mark. Folded stretches are tokens of their own,
 * drawn as chips.
 */
function marked(before, after, folds) {
  const tokens = after.match(TOKENS) || [];
  const words = (text) => (text.match(TOKENS) || []).filter((t) => !/^\s/.test(t));
  const kept = common(words(before), words(after)); // null: too long to compare, so nothing marked

  const nodes = [];
  let plain = '';
  let changed = '';
  let pending = ''; // whitespace after a changed word, not yet placed
  let w = 0;
  const flushPlain = () => {
    if (plain) nodes.push(el('span', 'w-same', plain));
    plain = '';
  };
  const flushChanged = () => {
    if (!changed) return;
    flushPlain();
    nodes.push(el('span', 'w-changed', changed));
    changed = '';
  };
  for (const token of tokens) {
    if (/^\s/.test(token)) {
      if (changed) pending += token;
      else plain += token;
      continue;
    }
    const same = !kept || kept.has(w);
    w++;
    const stretch = folds.stretch(token);
    if (stretch) {
      flushChanged();
      plain += pending;
      pending = '';
      flushPlain();
      nodes.push(foldChip(stretch, !same));
      continue;
    }
    if (same) {
      flushChanged();
      plain += pending + token;
    } else {
      changed += pending + token;
    }
    pending = '';
  }
  flushChanged();
  plain += pending;
  flushPlain();
  return nodes;
}

/** The indices into `b` of a longest subsequence it shares with `a`. */
function common(a, b) {
  const n = a.length;
  const m = b.length;
  if ((n + 1) * (m + 1) > MAX_DIFF_CELLS) return null;
  const width = m + 1;
  const table = new Uint16Array((n + 1) * width);
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      table[i * width + j] =
        a[i] === b[j]
          ? table[(i + 1) * width + j + 1] + 1
          : Math.max(table[(i + 1) * width + j], table[i * width + j + 1]);
    }
  }
  const kept = new Set();
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      kept.add(j);
      i++;
      j++;
    } else if (table[(i + 1) * width + j] >= table[i * width + j + 1]) {
      i++;
    } else {
      j++;
    }
  }
  return kept;
}
