// The agent's replies, rendered: the Markdown it writes, as the page shows it.
//
// Agents answer in Markdown — bold, lists, `code`, the odd heading — and shown
// as plain text that is a page of asterisks and backticks. This covers what
// they actually write, and no more: paragraphs, bullet and numbered lists
// (nested by indentation), headings, quotes, fenced code, and inline bold,
// italics, code and links.
//
// Everything is built as DOM nodes from text; nothing is ever parsed as HTML,
// so a reply cannot inject markup, whatever it quotes. Emphasis is strict
// about its edges, because replies are full of LaTeX: `$A_{t}$`, `$x_1$ and
// $y_2$` and `K^*` must stay as written rather than turn into italics.
//
// Math — `$…$`, `\(…\)`, and `$$…$$` or `\[…\]` for displays — is typeset
// with KaTeX, using the paper's own macros (see texai.macros). KaTeX loads the
// first time a reply has math, and until it has, or wherever it cannot parse
// something, the LaTeX is shown as written. Code spans and blocks are never
// typeset: `\eqref{…}` in backticks is code.

import { getJSON } from './api.js';

const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
};

const FENCE = /^\s*(```|~~~)/;
const HEADING = /^(#{1,6})\s+(.*?)\s*#*\s*$/;
const ITEM = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;
const QUOTE = /^\s*>\s?(.*)$/;
const RULE = /^\s*([-*_])(\s*\1){2,}\s*$/;
// A display on lines of its own: $$ … $$ or \[ … \].
const DISPLAY = /^\s*(\$\$|\\\[)/;
const DISPLAY_END = { '$$': '$$', '\\[': '\\]' };

// The math nodes of the render in progress, typeset once it is built.
let pendingMath = null;

// A source reference the chat can follow: `sections/model.tex:12` (or 12-15).
const REF = /^([\w./-]+\.tex):(\d+)(?:[-–]\d+)?$/;

/**
 * `text` rendered as a fragment. `onRef(file, line)`, when given, makes
 * `file.tex:line` code spans clickable references.
 */
export function renderMarkdown(text, { onRef } = {}) {
  const outer = pendingMath === null;
  if (outer) pendingMath = [];
  try {
    return renderBlocks(text, onRef);
  } finally {
    if (outer) {
      const math = pendingMath;
      pendingMath = null;
      if (math.length) typeset(math);
    }
  }
}

function renderBlocks(text, onRef) {
  const lines = String(text ?? '').replace(/\r\n?/g, '\n').split('\n');
  const fragment = document.createDocumentFragment();
  const inline = (s) => renderInline(s, onRef);
  let i = 0;
  let paragraph = [];

  const flush = () => {
    if (!paragraph.length) return;
    const p = el('p');
    paragraph.forEach((line, k) => {
      if (k) p.append(document.createElement('br'));
      p.append(...inline(line));
    });
    fragment.append(p);
    paragraph = [];
  };

  while (i < lines.length) {
    const line = lines[i];
    if (FENCE.test(line)) {
      flush();
      const marker = line.trim().slice(0, 3);
      const code = [];
      i += 1;
      while (i < lines.length && !lines[i].trim().startsWith(marker)) code.push(lines[i++]);
      i += 1; // the closing fence, if there is one
      const pre = el('pre', 'md-code');
      pre.append(el('code', null, code.join('\n')));
      fragment.append(pre);
      continue;
    }
    if (!line.trim()) {
      flush();
      i += 1;
      continue;
    }
    const display = line.match(DISPLAY);
    const found = display && readDisplay(lines, i, display[1]);
    if (found) {
      flush();
      fragment.append(mathNode(found.tex, true, lines.slice(i, found.next).join('\n')));
      i = found.next;
      continue;
    }
    const heading = line.match(HEADING);
    if (heading) {
      flush();
      const h = el('div', `md-h md-h${Math.min(heading[1].length, 3)}`);
      h.append(...inline(heading[2]));
      fragment.append(h);
      i += 1;
      continue;
    }
    if (RULE.test(line)) {
      flush();
      fragment.append(el('hr', 'md-rule'));
      i += 1;
      continue;
    }
    if (ITEM.test(line)) {
      flush();
      const [list, next] = parseList(lines, i, inline);
      fragment.append(list);
      i = next;
      continue;
    }
    if (QUOTE.test(line)) {
      flush();
      const quoted = [];
      while (i < lines.length && QUOTE.test(lines[i])) quoted.push(lines[i++].match(QUOTE)[1]);
      const block = el('blockquote', 'md-quote');
      block.append(renderMarkdown(quoted.join('\n'), { onRef }));
      fragment.append(block);
      continue;
    }
    paragraph.push(line.trim());
    i += 1;
  }
  flush();
  return fragment;
}

/** A list starting at line `start`, nested by indentation; returns it and the next line. */
function parseList(lines, start, inline) {
  const first = lines[start].match(ITEM);
  const indent = first[1].length;
  const ordered = /\d/.test(first[2]);
  const list = el(ordered ? 'ol' : 'ul', 'md-list');
  if (ordered && parseInt(first[2], 10) !== 1) list.start = parseInt(first[2], 10);
  let item = null;
  let i = start;

  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      // A blank line ends the list unless more of it follows, indented or not.
      const next = lines.slice(i + 1).find((l) => l.trim());
      if (next === undefined || !(ITEM.test(next) || /^\s{2,}\S/.test(next))) break;
      i += 1;
      continue;
    }
    const match = line.match(ITEM);
    if (match && match[1].length > indent && item) {
      const [nested, next] = parseList(lines, i, inline);
      item.append(nested);
      i = next;
      continue;
    }
    if (match && match[1].length === indent && /\d/.test(match[2]) === ordered) {
      item = el('li');
      item.append(...inline(match[3]));
      list.append(item);
      i += 1;
      continue;
    }
    if (!match && item && /^\s+\S/.test(line)) {
      // A continuation line of the item above.
      item.append(document.createElement('br'), ...inline(line.trim()));
      i += 1;
      continue;
    }
    break;
  }
  return [list, i];
}

// The inline spans, in the order they are tried at each position.
const SPANS = [
  { re: /`([^`\n]+)`/y, make: (m, onRef) => codeSpan(m[1], onRef) },
  // Math, never emphasis inside: $x_1^*$. A dollar amount is not math — the
  // pair must hug its contents, and the closing one not run into a digit.
  { re: /\$\$([^$]+?)\$\$/y, make: (m) => mathNode(m[1].trim(), true, m[0]) },
  { re: /\$(?=[^\s$])([^$\n]*?[^\s\\$])\$(?!\d)/y, make: (m) => mathNode(m[1], false, m[0]) },
  { re: /\$([^\s$\\])\$(?!\d)/y, make: (m) => mathNode(m[1], false, m[0]) },
  { re: /\\\(([\s\S]+?)\\\)/y, make: (m) => mathNode(m[1].trim(), false, m[0]) },
  { re: /\*\*(?=\S)([\s\S]*?\S)\*\*/y, make: (m, onRef) => wrap('strong', m[1], onRef) },
  { re: /__(?=\S)([\s\S]*?\S)__(?!\w)/y, make: (m, onRef) => wrap('strong', m[1], onRef), wordStart: true },
  { re: /\*(?=[^\s*])([^*\n]*?[^\s*])\*(?!\*)/y, make: (m, onRef) => wrap('em', m[1], onRef) },
  { re: /_(?=[^\s_])([^_\n]*?[^\s_])_(?!\w)/y, make: (m, onRef) => wrap('em', m[1], onRef), wordStart: true },
  { re: /\[([^\]\n]+)\]\(([^)\s]+)\)/y, make: (m, onRef) => link(m[1], m[2], onRef) },
];

/** Inline Markdown in `text`, as a list of nodes. */
function renderInline(text, onRef) {
  const nodes = [];
  let plain = '';
  let i = 0;
  while (i < text.length) {
    let matched = null;
    // A star after ^ or \ is LaTeX (K^*, \section*), not emphasis.
    // An escaped dollar is a dollar.
    const escaped = text[i] === '$' && text[i - 1] === '\\';
    if ('`*_[$\\'.includes(text[i]) && !escaped && !(text[i] === '*' && /[\^\\]/.test(text[i - 1] || ''))) {
      for (const span of SPANS) {
        // Underscores only open at the start of a word: x_1 is not emphasis.
        if (span.wordStart && i > 0 && /\w/.test(text[i - 1])) continue;
        span.re.lastIndex = i;
        const m = span.re.exec(text);
        if (m) {
          matched = { span, m };
          break;
        }
      }
    }
    if (!matched) {
      plain += text[i];
      i += 1;
      continue;
    }
    if (plain) nodes.push(document.createTextNode(plain));
    plain = '';
    nodes.push(matched.span.make(matched.m, onRef));
    i += matched.m[0].length;
  }
  if (plain) nodes.push(document.createTextNode(plain));
  return nodes;
}

function wrap(tag, inner, onRef) {
  const node = el(tag);
  node.append(...renderInline(inner, onRef));
  return node;
}

function codeSpan(code, onRef) {
  const ref = onRef && code.match(REF);
  if (ref) {
    const node = el('code', 'md-inline file ref', code);
    node.title = `Show ${ref[1]}:${ref[2]} in the PDF`;
    node.addEventListener('click', (event) => {
      event.stopPropagation();
      onRef(ref[1], Number(ref[2]));
    });
    return node;
  }
  return el('code', 'md-inline', code);
}

/** Only web links become links; anything else stays as the text it was. */
function link(label, url, onRef) {
  if (!/^https?:\/\//i.test(url)) return document.createTextNode(`[${label}](${url})`);
  const a = el('a', 'md-link');
  a.href = url;
  a.target = '_blank';
  a.rel = 'noopener noreferrer';
  a.append(...renderInline(label, onRef));
  return a;
}

/* ---------------- math ---------------- */

/** A math node showing its LaTeX as written, typeset once KaTeX is in. */
function mathNode(tex, display, raw) {
  const node = el(display ? 'div' : 'span', display ? 'md-math md-math-block' : 'md-math', raw);
  node.dataset.tex = tex;
  if (display) node.dataset.display = '1';
  pendingMath?.push(node);
  return node;
}

/**
 * A display opening on line `start` with `open`: its LaTeX and the line after
 * it, or null when it is not closed, or text follows the close on its line.
 */
function readDisplay(lines, start, open) {
  const end = DISPLAY_END[open];
  const body = [];
  for (let j = start; j < lines.length; j++) {
    const line = j === start ? lines[j].trimStart().slice(open.length) : lines[j];
    const at = line.indexOf(end);
    if (at >= 0) {
      if (line.slice(at + end.length).trim()) return null;
      body.push(line.slice(0, at));
      return { tex: body.join('\n').trim(), next: j + 1 };
    }
    body.push(line);
  }
  return null;
}

let katexLoad = null;

/** KaTeX and the paper's macros, loaded once, the first time there is math. */
function loadKatex() {
  if (!katexLoad) {
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = '/static/vendor/katex/katex.min.css';
    document.head.append(link);
    katexLoad = Promise.all([
      import('./vendor/katex/katex.mjs'),
      getJSON('/api/macros').then((r) => r.macros || {}).catch(() => ({})),
    ]).then(([module, macros]) => ({ katex: module.default, macros }));
  }
  return katexLoad;
}

async function typeset(nodes) {
  let loaded;
  try {
    loaded = await loadKatex();
  } catch {
    return; // no KaTeX: the LaTeX stays as written
  }
  const { katex, macros } = loaded;
  for (const node of nodes) {
    const raw = node.textContent;
    try {
      katex.render(node.dataset.tex, node, {
        displayMode: node.dataset.display === '1',
        // A copy each time: \gdef in one reply must not leak into the next.
        macros: { ...macros },
        throwOnError: true,
        strict: 'ignore',
        trust: false,
      });
      node.classList.add('md-math-done');
    } catch (err) {
      node.textContent = raw; // KaTeX may have emptied it before giving up
      node.title = `Not typeset: ${err.message || err}`;
    }
  }
}
