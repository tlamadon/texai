// What is in the source and not on the page: texai's own annotations and LaTeX
// comments, found in a piece of LaTeX so a view can fold them away to a chip.
// The inline editor folds them in its box, and the versions on offer fold them
// in their cards; both show or hide them by the same two switches.

// What is in the source and not on the page, folded away to a small chip
// unless asked for: texai's own annotations, and LaTeX comments.
export const HIDDEN = [
  { kind: 'notes', key: 'texai:inline-show-notes', label: '%@', title: "texai's annotations: %@ lines and %# markers" },
  { kind: 'comments', key: 'texai:inline-show-comments', label: '%', title: 'LaTeX comments' },
];

/** Whether stretches of `kind` ('notes' or 'comments') are shown rather than folded. */
export const foldShown = (kind) =>
  localStorage.getItem(HIDDEN.find((h) => h.kind === kind).key) === '1';

/** Where a line's comment starts (its first unescaped %), or -1. */
export function commentAt(text) {
  for (let i = text.indexOf('%'); i !== -1; i = text.indexOf('%', i + 1)) {
    let slashes = 0;
    while (i - slashes > 0 && text[i - slashes - 1] === '\\') slashes++;
    if (slashes % 2 === 0) return i;
  }
  return -1;
}

/**
 * The stretches of a piece of LaTeX that never reach the page, as
 * `{ kind, from, to }` in CodeMirror positions: runs of comment-only lines
 * (taken with their line breaks, so no blank is left behind) and comments at
 * the end of a line of text. `kind` is 'notes' for what texai writes (`%@`,
 * its `%>` continuations, `%#` markers) and 'comments' for the rest.
 */
export function unrendered(text) {
  const lines = text.split('\n');
  const found = [];
  let run = null;
  const close = () => {
    if (!run) return;
    const { kind, first, last } = run;
    run = null;
    if (last < lines.length - 1) {
      found.push({ kind, from: { line: first, ch: 0 }, to: { line: last + 1, ch: 0 } });
    } else if (first > 0) {
      // Nothing after it to pull up: fold it into the end of the line before.
      found.push({
        kind,
        from: { line: first - 1, ch: lines[first - 1].length },
        to: { line: last, ch: lines[last].length },
      });
    }
    // All there is: leave it showing, or the box would look empty.
  };
  lines.forEach((line, i) => {
    const at = commentAt(line);
    if (at !== -1 && !line.slice(0, at).trim()) {
      const kind = /^%[@>#]/.test(line.slice(at)) ? 'notes' : 'comments';
      if (run && run.kind === kind) run.last = i;
      else {
        close();
        run = { kind, first: i, last: i };
      }
      return;
    }
    close();
    // A bare % only joins two lines; there is nothing in it to hide.
    if (at === -1 || line.slice(at).trim() === '%') return;
    const kind = /^%#/.test(line.slice(at)) ? 'notes' : 'comments';
    const ch = line.slice(0, at).trimEnd().length;
    found.push({ kind, from: { line: i, ch }, to: { line: i, ch: line.length } });
  });
  close();
  return found;
}

/** The same stretches as `unrendered`, with `from` and `to` as offsets into `text`. */
export function unrenderedSpans(text) {
  const starts = [0];
  for (let i = text.indexOf('\n'); i !== -1; i = text.indexOf('\n', i + 1)) starts.push(i + 1);
  const offset = ({ line, ch }) => (line < starts.length ? starts[line] + ch : text.length);
  return unrendered(text).map(({ kind, from, to }) => ({ kind, from: offset(from), to: offset(to) }));
}

/** The comments of a piece of LaTeX as [from, to) offsets: from each `%` to the end of its line. */
export function commentSpans(text) {
  const spans = [];
  let start = 0;
  for (const line of text.split('\n')) {
    const at = commentAt(line);
    if (at !== -1) spans.push([start + at, start + line.length]);
    start += line.length + 1;
  }
  return spans;
}
