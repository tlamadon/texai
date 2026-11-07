// Versions of a passage the agent offered instead of editing it, laid out for
// picking one.
//
// Each version shows the source it would put in place (raw LaTeX for now),
// with the words it changes marked: versions of one sentence often differ by a
// few words, and those are hard to spot side by side. The pick itself is the
// caller's — the box on the page and the chat card both send it to
// /api/turns/<id>/pick, where texai puts the version in place.

const MAX_DIFF_CELLS = 250_000; // past this, show the text unmarked

const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};

/**
 * One offer as a row of cards, one per version. `onPick(i)` runs when version
 * `i` is chosen; `compact` stacks them, for the narrow chat panel.
 */
export function renderOffer(offer, { onPick, compact = false } = {}) {
  const root = el('div', compact ? 'offer compact' : 'offer');
  if (offer.note) root.append(el('div', 'offer-note', offer.note));
  const row = el('div', 'offer-options');
  offer.options.forEach((option, i) => {
    const card = el('div', 'offer-option');
    card.tabIndex = 0;
    card.setAttribute('role', 'button');
    card.title = `Use this version (${i + 1})`;
    card.classList.toggle('picked', offer.picked === i);
    const head = el('div', 'offer-label');
    head.append(el('span', 'offer-key', String(i + 1)), el('span', '', option.label));
    const text = el('div', 'offer-text');
    text.append(...marked(offer.original, option.text));
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
  return root;
}

/** Show version `i` of a rendered offer as the one chosen, the rest set aside. */
export function markChosen(node, i) {
  node.querySelectorAll('.offer-option').forEach((card, j) => {
    card.classList.toggle('chosen', i === j);
    card.classList.toggle('picked', i === j);
  });
}

/**
 * `after` as text nodes, with the words not in `before` wrapped in <mark>. A
 * word-level longest common subsequence: whitespace never counts as a change,
 * and the space between two changed words joins them into one mark.
 */
function marked(before, after) {
  const tokens = after.match(/\s+|\S+/g) || [];
  const words = (text) => text.match(/\S+/g) || [];
  const old = words(before);
  const now = tokens.filter((t) => !/^\s/.test(t));
  const kept = common(old, now);
  if (!kept) return [document.createTextNode(after)];

  const nodes = [];
  let plain = '';
  let changed = '';
  let pending = ''; // whitespace after a changed word, not yet placed
  let w = 0;
  const flushChanged = () => {
    if (!changed) return;
    nodes.push(document.createTextNode(plain));
    nodes.push(el('mark', 'offer-ins', changed));
    plain = '';
    changed = '';
  };
  for (const token of tokens) {
    if (/^\s/.test(token)) {
      if (changed) pending += token;
      else plain += token;
      continue;
    }
    if (kept.has(w++)) {
      flushChanged();
      plain += pending + token;
    } else {
      changed += pending + token;
    }
    pending = '';
  }
  flushChanged();
  plain += pending;
  if (plain) nodes.push(document.createTextNode(plain));
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
