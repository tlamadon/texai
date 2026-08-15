// The shape of the document, read out of the buffer.
//
// LaTeX has no outline of its own — \section is a macro like any other — so the
// headings are found by reading the source. Deliberately the buffer and not the
// file on disk: while there are unsaved edits the two disagree about which line
// a heading is on, and an outline that jumps to the wrong line is worse than no
// outline at all. A heading typed a second ago is in the list, at the line it is
// really on.

const LEVELS = {
  part: 0,
  chapter: 1,
  section: 2,
  subsection: 3,
  subsubsection: 4,
  frame: 4,
  paragraph: 5,
  subparagraph: 6,
};

// \input and \include are not headings but they are how a paper is put
// together, so they belong in the same list — as links to the file they pull in.
const ENTRY_RE =
  /\\(part|chapter|section|subsection|subsubsection|paragraph|subparagraph|frametitle|input|include|begin)\b\*?\s*(?:\[[^\]]*\])?\s*\{/g;

// Things a reader looks for by name rather than by section: floats go in under
// their caption, theorem-likes under their title or their label.
const FLOATS = new Map([
  ['figure', 'Figure'],
  ['wrapfigure', 'Figure'],
  ['sidewaysfigure', 'Figure'],
  ['table', 'Table'],
  ['wraptable', 'Table'],
  ['sidewaystable', 'Table'],
  ['longtable', 'Table'],
  ['algorithm', 'Algorithm'],
]);

// Theorem environments are declared, not built in, so \newtheorem is read as it
// is met (see learnTheorems). These are the names papers use without thinking,
// which covers the file you opened before the one declaring them was read.
const THEOREMS = new Map([
  ['theorem', 'Theorem'], ['thm', 'Theorem'],
  ['lemma', 'Lemma'], ['lem', 'Lemma'],
  ['proposition', 'Proposition'], ['prop', 'Proposition'],
  ['corollary', 'Corollary'], ['cor', 'Corollary'],
  ['definition', 'Definition'], ['defn', 'Definition'], ['dfn', 'Definition'],
  ['assumption', 'Assumption'], ['assum', 'Assumption'],
  ['claim', 'Claim'],
  ['conjecture', 'Conjecture'], ['conj', 'Conjecture'],
  ['axiom', 'Axiom'],
  ['hypothesis', 'Hypothesis'], ['hyp', 'Hypothesis'],
  ['remark', 'Remark'], ['rem', 'Remark'],
  ['example', 'Example'], ['exmp', 'Example'],
  ['problem', 'Problem'],
  ['question', 'Question'],
  ['notation', 'Notation'],
]);

const NEWTHEOREM_RE = /\\newtheorem\*?\s*\{([^}]+)\}\s*(?:\[[^\]]*\])?\s*\{([^}]+)\}/g;
const CAPTION_RE = /\\caption\s*(?:\[[^\]]*\])?\s*\{/g;
const LABEL_RE = /\\label\s*\{([^}]*)\}/g;

// A group can run past the end of a file that is missing a brace; scanning the
// whole rest of the document for every entry would then be quadratic.
const MAX_GROUP = 2000;
// How far past \begin{...} to look for the caption or label that names it.
const MAX_ENV = 8000;
const MAX_TITLE = 80;

/** Read the balanced ``{...}`` starting at ``open``, or null if it never closes. */
function readGroup(text, open) {
  const limit = Math.min(text.length, open + MAX_GROUP);
  let depth = 0;
  for (let i = open; i < limit; i += 1) {
    const ch = text[i];
    if (ch === '\\') i += 1;
    else if (ch === '{') depth += 1;
    else if (ch === '}' && --depth === 0) return { body: text.slice(open + 1, i), end: i };
  }
  return null;
}

/** Is this position inside a comment — i.e. past an unescaped % on its line? */
function isCommented(text, lineStart, index) {
  for (let i = lineStart; i < index; i += 1) {
    if (text[i] === '\\') i += 1;
    else if (text[i] === '%') return true;
  }
  return false;
}

/** Ranges of verbatim-ish environments, where a \section is a printed example. */
function verbatimRanges(text) {
  const ranges = [];
  const re = /\\begin\s*\{(verbatim\*?|lstlisting|minted|Verbatim)\}/g;
  for (let m = re.exec(text); m; m = re.exec(text)) {
    const close = text.indexOf(`\\end{${m[1]}}`, m.index);
    ranges.push([m.index, close === -1 ? text.length : close]);
    re.lastIndex = close === -1 ? text.length : close;
  }
  return ranges;
}

/** Turn a heading's argument into something readable in a narrow column. */
function cleanTitle(raw) {
  return raw
    .replace(/\\label\s*\{[^}]*\}/g, '')
    .replace(/\\(?:footnote|thanks|protect)\s*\{[^}]*\}/g, '')
    .replace(/\\LaTeX\b/g, 'LaTeX')
    .replace(/\\TeX\b/g, 'TeX')
    .replace(/\\\\/g, ' ')
    // Formatting macros keep their argument; anything else is dropped whole.
    .replace(/\\(?:texttt|textit|textbf|textsc|emph|text|mathrm|mbox)\s*\{([^{}]*)\}/g, '$1')
    .replace(/\\[a-zA-Z]+\*?\s*/g, ' ')
    .replace(/[{}~]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

/** First match of ``re`` between two offsets, or null. */
function findBetween(text, re, from, to) {
  re.lastIndex = from;
  const match = re.exec(text);
  return match && match.index < to ? match : null;
}

function shorten(title) {
  return title.length > MAX_TITLE ? `${title.slice(0, MAX_TITLE - 1).trimEnd()}…` : title;
}

/**
 * What to call an environment: its caption, its \[title], or its label.
 *
 * A caption usually sits at the far end of a float — under the tabular, after
 * fifty lines of numbers — so the search runs to \end, not to the next line.
 */
function nameEnvironment(text, environment, from, titled) {
  const closing = text.indexOf(`\\end{${environment}}`, from);
  const to = Math.min(closing === -1 ? text.length : closing, from + MAX_ENV);

  if (titled) {
    // \begin{theorem}[Existence and uniqueness]
    const optional = /^\s*\[/.exec(text.slice(from + 1, to));
    if (optional) {
      const close = text.indexOf(']', from + 1 + optional[0].length - 1);
      if (close !== -1 && close < to) {
        return shorten(cleanTitle(text.slice(from + 1 + optional[0].length, close)));
      }
    }
  } else {
    const caption = findBetween(text, CAPTION_RE, from, to);
    if (caption) {
      const group = readGroup(text, CAPTION_RE.lastIndex - 1);
      if (group) return shorten(cleanTitle(group.body));
    }
  }

  const label = findBetween(text, LABEL_RE, from, to);
  return label ? label[1].trim() : '';
}

/** Remember the theorem environments a document declares for itself. */
export function learnTheorems(text) {
  NEWTHEOREM_RE.lastIndex = 0;
  for (let m = NEWTHEOREM_RE.exec(text); m; m = NEWTHEOREM_RE.exec(text)) {
    const printed = cleanTitle(m[2]);
    if (printed) THEOREMS.set(m[1].trim(), printed);
  }
}

/**
 * The headings and \input lines of a LaTeX document, in source order.
 *
 * Each entry is `{ line, level, kind, title, target }`, where `target` is set
 * only for \input and \include. Levels are normalised so that a paper whose
 * deepest command is \section starts flush against the left edge — pass
 * `normalize: false` when the result is to be spliced into a larger document,
 * which has to do its own normalising across all the files at once.
 */
export function parseOutline(text, { normalize = true } = {}) {
  if (!text) return [];
  learnTheorems(text);
  const skip = verbatimRanges(text);
  const entries = [];
  // Floats and theorems hang under whatever heading they fall in.
  let under = LEVELS.section;

  // Matches arrive in increasing order, so the line number can be carried
  // along instead of recomputed from the start of the file each time.
  let line = 1;
  let lineStart = 0;
  let cursor = 0;
  const advanceTo = (index) => {
    while (cursor < index) {
      if (text[cursor] === '\n') {
        line += 1;
        lineStart = cursor + 1;
      }
      cursor += 1;
    }
  };

  ENTRY_RE.lastIndex = 0;
  for (let match = ENTRY_RE.exec(text); match; match = ENTRY_RE.exec(text)) {
    const open = ENTRY_RE.lastIndex - 1;
    advanceTo(match.index);
    if (isCommented(text, lineStart, match.index)) continue;
    if (skip.some(([from, to]) => match.index > from && match.index < to)) continue;

    let command = match[1];
    let group = readGroup(text, open);
    if (!group) continue;

    // Most environments are just structure; the ones a reader navigates by are
    // slides, floats and theorems.
    if (command === 'begin') {
      const environment = group.body.trim();
      const base = environment.replace(/\*$/, '');

      if (base === 'frame') {
        const rest = /^\s*(?:<[^>]*>)?\s*(?:\[[^\]]*\])?\s*\{/.exec(text.slice(group.end + 1));
        if (!rest) continue;
        group = readGroup(text, group.end + rest[0].length);
        if (!group) continue;
        command = 'frametitle';
      } else if (FLOATS.has(base) || THEOREMS.has(base)) {
        const theorem = !FLOATS.has(base);
        const printed = theorem ? THEOREMS.get(base) : FLOATS.get(base);
        const named = nameEnvironment(text, environment, group.end, theorem);
        entries.push({
          line,
          level: under + 1,
          kind: theorem ? 'theorem' : 'float',
          title: named ? `${printed}: ${named}` : printed,
          // What to look for on the page is the caption itself; "Table:" is our
          // word for it, and "Table 1:" is LaTeX's.
          phrase: named || null,
          target: null,
        });
        continue;
      } else {
        continue;
      }
    }

    if (command === 'input' || command === 'include') {
      const target = group.body.trim();
      if (!target) continue;
      entries.push({
        line,
        level: LEVELS.section,
        kind: 'input',
        title: target,
        phrase: null,
        target,
      });
      continue;
    }

    const title = cleanTitle(group.body);
    const kind = command === 'frametitle' ? 'frame' : command;
    under = LEVELS[kind];
    entries.push({
      line,
      level: under,
      kind,
      title: title || '(untitled)',
      phrase: title || null,
      target: null,
    });
  }

  if (!normalize) return entries;
  const headings = entries.filter((entry) => entry.kind !== 'input');
  const shallowest = headings.length ? Math.min(...headings.map((e) => e.level)) : 0;
  for (const entry of entries) entry.level = Math.max(0, entry.level - shallowest);
  return entries;
}

/** The entry a line sits in: the last one at or above it. */
export function entryAt(entries, line) {
  let found = null;
  for (const entry of entries) {
    if (entry.line > line) break;
    if (entry.kind !== 'input') found = entry;
  }
  return found;
}

/* ---------------- the column ---------------- */

const KIND_LABEL = {
  input: 'file',
  frame: 'frame',
  float: 'float',
  theorem: 'statement',
};

/**
 * A clickable outline in a column.
 *
 * Two of these exist: one beside the editor, listing the file being edited and
 * moving the cursor, and one beside the PDF, listing the whole document and
 * moving the page. Same list, different destination, so the element ids and the
 * pick handler are passed in.
 */
export class OutlinePanel {
  constructor({
    panel,
    list,
    toggle,
    storageKey,
    empty,
    defaultVisible = true,
    onPick,
    onOpenFile,
  } = {}) {
    this.els = {
      panel: document.getElementById(panel),
      list: document.getElementById(list),
      toggle: toggle ? document.getElementById(toggle) : null,
    };
    this.storageKey = storageKey;
    this.emptyText = empty || 'No sections in this file.';
    this.onPick = onPick || null;
    this.onOpenFile = onOpenFile || null;
    this.entries = [];
    this.active = null;
    this.rows = [];

    this.els.toggle?.addEventListener('click', () => this.setVisible(!this.visible));
    const stored = localStorage.getItem(this.storageKey);
    this.setVisible(stored === null ? defaultVisible : stored === '1');
  }

  setVisible(visible) {
    this.visible = visible;
    this.els.panel.hidden = !visible;
    this.els.toggle?.classList.toggle('on', visible);
    this.els.toggle?.setAttribute('aria-pressed', String(visible));
    localStorage.setItem(this.storageKey, visible ? '1' : '0');
    this.onToggle?.(visible);
  }

  setEntries(entries) {
    this.entries = entries;
    this.rows = [];
    this.active = null;

    if (!entries.length) {
      const empty = document.createElement('div');
      empty.className = 'outline-empty';
      empty.textContent = this.emptyText;
      this.els.list.replaceChildren(empty);
      return;
    }

    this.els.list.replaceChildren(
      ...entries.map((entry) => {
        const row = document.createElement('button');
        row.type = 'button';
        row.className = `outline-row lvl-${Math.min(entry.level, 4)} kind-${entry.kind}`;
        row.style.paddingLeft = `${8 + entry.level * 10}px`;
        // The column is narrow, so an \input shows the file it pulls in rather
        // than the directory it lives in; the full path is on the tooltip.
        row.textContent = entry.kind === 'input' ? entry.title.split('/').pop() : entry.title;
        row.title = `${entry.file ? `${entry.file}:${entry.line}` : `line ${entry.line}`} — ${
          KIND_LABEL[entry.kind] || entry.kind
        }`;
        row.addEventListener('click', (event) => {
          if (entry.kind === 'input') this.onOpenFile?.(entry.target);
          else this.onPick?.(entry, event);
        });
        this.rows.push(row);
        return row;
      })
    );
  }

  /** Mark the heading the cursor is inside, and keep it in view. */
  setActiveLine(line) {
    const entry = entryAt(this.entries, line);
    const index = entry ? this.entries.indexOf(entry) : null;
    if (index === this.active) return;
    if (this.active != null) this.rows[this.active]?.classList.remove('active');
    this.active = index;
    if (this.active == null) return;
    const row = this.rows[this.active];
    row.classList.add('active');
    // 'nearest' is a no-op when the row is already on screen, so a cursor
    // wandering inside one section never tugs the column around.
    row.scrollIntoView({ block: 'nearest' });
  }
}
