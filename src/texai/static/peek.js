// The Peek tab: what backs a sentence, opened beside the page.
//
// A `%@src` marker in the margin (see annotations.js) names a source; the
// server says how to show it (provenance.py). A paper or a project PDF opens in
// a viewer of its own, so the page being read stays where it was. With a quote,
// the viewer finds those words in the paper's text and highlights them; the page
// number is only where it looks first, since a paper's pages move between
// versions and its words do not. A label opens this paper's own PDF at the
// labelled table or equation; a text file opens at its lines.
//
// A `%@propose` choice opens here too: its versions side by side, the words
// each would change marked, to pick one or dismiss them all.

import { postJSON } from './api.js';
import { describeSource, shortAuthors, sourceTarget } from './annotations.js';
import { markChosen, renderOffer } from './options.js';
import { PdfViewer } from './viewer.js';

const KIND = { label: 'This paper', file: 'Project file', paper: 'Paper' };
// Below this many letters a quote matches too much to mean anything.
const MIN_QUOTE = 6;
// When the exact words are not there (hyphenation, a ligature the PDF spelled
// oddly, a typo in the quote), look for a run of them this long instead. The
// agent's read_paper tool (papers.py) shows it the PDF's own wording, so the
// quotes it writes match exactly, and this is only the fallback for the rest.
const PROBE = 32;

const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};

/**
 * Text reduced to what survives any PDF's text extraction: letters and digits,
 * lower-cased, accents and ligatures unfolded. Spaces, hyphens and punctuation
 * all go, so "elas-\nticity" and "elasticity" agree.
 */
const foldChar = (ch) =>
  ch
    .normalize('NFKD')
    .replace(/\p{M}/gu, '')
    .toLowerCase()
    .replace(/[^\p{L}\p{N}]/gu, '');
const fold = (text) => [...text].map(foldChar).join('');

export class PeekPanel {
  constructor() {
    this.els = {
      pane: document.getElementById('peek'),
      kind: document.getElementById('peek-kind'),
      title: document.getElementById('peek-title'),
      detail: document.getElementById('peek-detail'),
      status: document.getElementById('peek-status'),
      actions: document.getElementById('peek-actions'),
      empty: document.getElementById('peek-empty'),
      pdf: document.getElementById('peek-pdf'),
      viewer: document.getElementById('peek-viewer'),
      text: document.getElementById('peek-text'),
      image: document.getElementById('peek-image'),
      proposal: document.getElementById('peek-proposal'),
    };
    this.onEdit = null; // (file, line): open the annotation in the editor
    this.onShowInPaper = null; // (location): move the main page to a labelled spot
    this.pdfUrl = () => '/api/pdf'; // this paper's PDF, as currently built
    this.token = 0;
    this.loadedUrl = null;
    this.hits = null; // {page, rects(entry) -> page-pixel rects}

    this.viewer = new PdfViewer({
      container: this.els.pdf,
      viewerEl: this.els.viewer,
      onRender: (entry) => this._drawHits(entry),
    });
    // The pane is resized by the splitter and hidden by other tabs; a fitted
    // page follows the first and ignores the second.
    new ResizeObserver(() => {
      if (this.els.pdf.hidden || !this.els.pdf.clientWidth || !this.viewer.doc) return;
      if (this.viewer.fitWidth) this.viewer.setFitWidth();
    }).observe(this.els.pdf);
  }

  /** Peek at what an annotation item's `%@src` points at, or at a proposal. */
  async show(item) {
    if (item.place === 'proposal') {
      this._showProposal(item);
      return;
    }
    const token = ++this.token;
    const { source } = item;
    this.item = item;
    this._header(source, null);
    this._status('Looking it up…');
    let answer;
    try {
      // The source as the margin already has it, parsed by the server: the
      // annotation's own text is only its free-text note.
      answer = await postJSON('/api/provenance', { source });
    } catch (err) {
      if (token !== this.token) return;
      this._only(null);
      this._status(err.message || String(err), 'error');
      return;
    }
    if (token !== this.token) return;
    this._header(answer.source, answer);

    if (answer.view === 'pdf') await this._showPdf(answer, token);
    else if (answer.view === 'text') this._showText(answer);
    else if (answer.view === 'image') this._showImage(answer);
    else {
      this._only(null);
      this._status(answer.problem || 'Nothing to show.', 'error');
    }
  }

  /* ---------------- header ---------------- */

  _header(source, answer) {
    const { kind, title, detail, actions } = this.els;
    kind.textContent = KIND[source.type] || 'Source';
    kind.className = `peek-kind s-${source.type}`;

    const bib = answer?.bib;
    title.textContent = bib?.title || sourceTarget(source);
    title.title = describeSource(source);

    detail.replaceChildren();
    const meta = [];
    if (bib) {
      const who = [bib.author && shortAuthors(bib.author), bib.year].filter(Boolean).join(', ');
      if (who) meta.push(who);
      meta.push(sourceTarget(source));
    }
    if (answer?.path && source.type !== 'file') meta.push(answer.path);
    if (meta.length) detail.append(el('div', 'peek-meta', meta.join(' · ')));
    if (source.quote) detail.append(el('blockquote', 'peek-quote', source.quote));
    if (source.note) detail.append(el('div', 'peek-note', source.note));

    actions.replaceChildren();
    const button = (label, title, onClick) => {
      const node = el('button', 'ghost', label);
      node.type = 'button';
      node.title = title;
      node.addEventListener('click', onClick);
      actions.append(node);
    };
    const { file, line } = this.item;
    button('Edit', `Open ${file}:${line}, where this %@src is written`, () =>
      this.onEdit?.(file, line)
    );
    if (answer?.view === 'pdf' && source.type === 'label' && answer.boxes?.length) {
      button('Go there', 'Move the page to it', () =>
        this.onShowInPaper?.({ found: true, file: answer.file, line: answer.line, page: answer.page, boxes: answer.boxes })
      );
    }
    if (answer?.view === 'pdf') {
      // The pane is narrow and a fitted page small; the passage is worth a zoom.
      // Zooming keeps the top of the view; the highlight is what was being read.
      const zoom = (how) => () => {
        how();
        this._recentre();
      };
      button('\u2212', 'Zoom out', zoom(() => this.viewer.zoomOut()));
      button('+', 'Zoom in', zoom(() => this.viewer.zoomIn()));
      button('Fit', 'Fit the page to the pane', zoom(() => this.viewer.setFitWidth()));
    }
    if (answer?.view === 'pdf' && source.type !== 'label') {
      const link = el('a', 'ghost button-link', 'Open ↗');
      link.href = `${answer.url}#page=${answer.page || source.page || 1}`;
      link.target = '_blank';
      link.rel = 'noopener';
      link.title = 'Open the PDF in a browser tab';
      actions.append(link);
    }
  }

  _status(text, tone = '') {
    const { status } = this.els;
    status.hidden = !text;
    status.textContent = text || '';
    status.className = `peek-status${tone ? ` ${tone}` : ''}`;
  }

  /** Show one of the bodies, or none (the placeholder). */
  _only(which) {
    this.els.empty.hidden = which !== null;
    this.els.pdf.hidden = which !== 'pdf';
    this.els.text.hidden = which !== 'text';
    this.els.image.hidden = which !== 'image';
    this.els.proposal.hidden = which !== 'proposal';
  }

  /* ---------------- a proposal ---------------- */

  _showProposal(item) {
    const token = ++this.token;
    this.item = item;
    const { kind, title, detail, actions, proposal } = this.els;
    const count = item.options.length;
    kind.textContent = 'Proposal';
    kind.className = 'peek-kind s-proposal';
    title.textContent = `${count} versions of ${item.file}:${item.start}`;
    title.title = '';
    detail.replaceChildren();
    const authors = [...new Set(item.options.map((option) => option.by).filter(Boolean))];
    const meta = [item.id && `id:${item.id}`, authors.length && `by ${authors.join(', ')}`];
    if (meta.some(Boolean)) detail.append(el('div', 'peek-meta', meta.filter(Boolean).join(' · ')));

    const settle = async (option) => {
      if (token !== this.token) return;
      this._status(option === null ? 'Dismissing…' : 'Putting it in place…', '');
      try {
        await postJSON('/api/proposals', {
          file: item.file,
          id: item.id || '',
          line: item.line,
          ...(option === null ? {} : { option }),
        });
      } catch (err) {
        if (token !== this.token) return;
        if (option !== null) markChosen(offer, -1);
        this._status(err.message || String(err), 'error');
        return;
      }
      if (token !== this.token) return;
      this._status(
        option === null
          ? 'Dismissed; the text stays as it is. The page rebuilds when it is done.'
          : `Version ${option + 1} goes in; the page rebuilds when it is done.`,
        'ok'
      );
    };

    actions.replaceChildren();
    const button = (label, hint, onClick) => {
      const node = el('button', 'ghost', label);
      node.type = 'button';
      node.title = hint;
      node.addEventListener('click', onClick);
      actions.append(node);
    };
    button('Edit', `Open ${item.file}:${item.line}, where the versions are written`, () =>
      this.onEdit?.(item.file, item.line)
    );
    button('Dismiss', 'Remove the versions and keep the text as it is', () => settle(null));

    const offer = renderOffer(
      { original: item.original, options: item.options },
      {
        compact: true,
        onPick: (i) => {
          markChosen(offer, i);
          settle(i);
        },
      }
    );
    const current = el('div', 'peek-current');
    current.append(el('div', 'peek-meta', 'Now'), el('div', 'offer-text', item.original));
    proposal.replaceChildren(current, offer);
    this._only('proposal');
    this._status('Pick a version to put it in place.', '');
  }

  /* ---------------- a PDF ---------------- */

  async _showPdf(answer, token) {
    const { source } = answer;
    this._only('pdf');
    const url = source.type === 'label' ? this.pdfUrl() : answer.url;
    this.hits = null;
    this._drawHits(null);
    if (url !== this.loadedUrl) {
      try {
        await this.viewer.load(url);
      } catch (err) {
        if (token !== this.token) return;
        this.loadedUrl = null;
        this._status(`Could not open the PDF: ${err.message || err}`, 'error');
        return;
      }
      if (token !== this.token) return;
      this.loadedUrl = url;
    }

    const problem = answer.problem ? `${answer.problem} ` : '';
    const moved = answer.moved ? `Found by its hash as ${answer.path}. ` : '';

    if (source.type === 'label') {
      if (answer.page && answer.boxes?.length) {
        // SyncTeX answers with every box the lines made (a float, its rows, its
        // caption); one outline around them all reads as "this".
        const boxes = answer.boxes.filter((box) => box.page === answer.page);
        const x = Math.min(...boxes.map((b) => b.x));
        const y = Math.min(...boxes.map((b) => b.y));
        const union = {
          x,
          y,
          width: Math.max(...boxes.map((b) => b.x + b.width)) - x,
          height: Math.max(...boxes.map((b) => b.y + b.height)) - y,
        };
        this._highlight(answer.page, (entry) => [this.viewer.pdfRectToPageRect(entry, union)]);
      }
      this._status(problem, problem ? 'warn' : '');
      return;
    }

    const page = clampPage(source.page || answer.page || 1, this.viewer.pageCount);
    if (!source.quote) {
      this.viewer.scrollToPage(page);
      this._status(`${moved}${problem}`, problem ? 'warn' : '');
      return;
    }

    this._status(`${moved}Finding the quote…`);
    const found = await this._findQuote(source.quote, page);
    if (token !== this.token) return;
    if (!found) {
      this.viewer.scrollToPage(page);
      this._status(
        `${moved}${problem}The quote is not in this PDF's text${source.page ? `; showing p. ${page}` : ''}.`,
        'warn'
      );
      return;
    }
    this._highlight(found.page, (entry) =>
      found.rects.map((rect) => userRectToPage(entry, rect))
    );
    const where = `Quote found on p. ${found.page}${found.approximate ? ' (close match)' : ''}`;
    const drift = source.page && found.page !== source.page ? `, not p. ${source.page} as written` : '';
    this._status(`${moved}${problem}${where}${drift}.`, problem || drift ? 'warn' : 'ok');
  }

  /** Mark a spot on a page, scroll to it, and keep it through re-renders. */
  _highlight(page, rects) {
    this.hits = { page, rects };
    const entry = this.viewer.pageEntry(page);
    if (!entry?.viewport) return;
    const first = rects(entry)[0];
    if (first) this.viewer.scrollToRect(entry, first, { align: 0.3 });
    this._drawHits(entry);
  }

  _recentre() {
    const entry = this.hits && this.viewer.pageEntry(this.hits.page);
    if (!entry?.viewport) return;
    const [first] = this.hits.rects(entry);
    if (!first) return;
    this.viewer.scrollToRect(entry, first, { align: 0.3 });
    // Sideways only when the highlight is off screen, and then to its middle.
    const { container } = this.viewer;
    const left = first.left + entry.el.offsetLeft - container.scrollLeft;
    if (left < 0 || left + first.width > container.clientWidth) {
      container.scrollLeft += left + first.width / 2 - container.clientWidth / 2;
    }
  }

  _drawHits(entry) {
    for (const page of this.viewer.pages) {
      if (entry && page !== entry) continue;
      page.el.querySelector('.peek-hits')?.remove();
      if (!this.hits || this.hits.page !== page.num || !page.viewport) continue;
      const layer = el('div', 'peek-hits');
      for (const rect of this.hits.rects(page)) {
        const node = el('div', 'peek-hit');
        node.style.left = `${rect.left}px`;
        node.style.top = `${rect.top}px`;
        node.style.width = `${rect.width}px`;
        node.style.height = `${rect.height}px`;
        layer.append(node);
      }
      page.el.append(layer);
    }
  }

  /**
   * Where a quote is in the loaded PDF: the page, and a rectangle per text run
   * it covers, in PDF user space. Pages are tried nearest the hint first.
   */
  async _findQuote(quote, hint) {
    const needle = fold(quote);
    if (needle.length < MIN_QUOTE) return null;
    const pages = this.viewer.pages;
    const order = [...pages].sort(
      (a, b) => Math.abs(a.num - hint) - Math.abs(b.num - hint) || a.num - b.num
    );

    const indexed = [];
    for (const entry of order) {
      const index = await this._pageText(entry);
      indexed.push([entry, index]);
      const at = index.text.indexOf(needle);
      if (at !== -1) return this._match(entry, index, at, needle.length, false);
    }

    // No exact match anywhere: try a few runs of the quote, start, middle and
    // end, and take the first that is found as the anchor for the whole.
    const probe = Math.min(PROBE, needle.length);
    const offsets = [0, Math.floor((needle.length - probe) / 2), needle.length - probe];
    for (const offset of offsets) {
      const run = needle.slice(offset, offset + probe);
      for (const [entry, index] of indexed) {
        const at = index.text.indexOf(run);
        if (at === -1) continue;
        const start = Math.max(0, at - offset);
        const length = Math.min(needle.length, index.text.length - start);
        return this._match(entry, index, start, length, true);
      }
    }
    return null;
  }

  async _pageText(entry) {
    if (entry.peekIndex) return entry.peekIndex;
    const content = await entry.page.getTextContent();
    let text = '';
    // For each folded character: the text item it came from, and where in
    // that item, as a fraction of its width.
    const owners = [];
    content.items.forEach((item, i) => {
      const chars = [...(item.str || '')];
      chars.forEach((ch, k) => {
        for (const c of foldChar(ch)) {
          text += c;
          owners.push([i, k / chars.length, (k + 1) / chars.length]);
        }
      });
    });
    entry.peekIndex = { text, owners, items: content.items };
    return entry.peekIndex;
  }

  _match(entry, index, start, length, approximate) {
    const spans = new Map(); // item -> [from, to] fractions of its width
    for (let i = start; i < start + length; i += 1) {
      const [item, from, to] = index.owners[i];
      const span = spans.get(item);
      if (span) span[1] = Math.max(span[1], to);
      else spans.set(item, [from, to]);
    }
    const rects = [];
    for (const [i, [from, to]] of spans) {
      const item = index.items[i];
      const [, , c, d, e, f] = item.transform;
      const size = Math.hypot(c, d) || item.height || 10;
      rects.push([e + item.width * from, f - size * 0.25, e + item.width * to, f + size * 0.85]);
    }
    return { page: entry.num, rects, approximate };
  }

  /* ---------------- a text file, an image ---------------- */

  _showText(answer) {
    const { source } = answer;
    this._only('text');
    const { text } = this.els;
    text.replaceChildren();
    const lines = answer.text.split('\n');
    let first = null;
    lines.forEach((line, i) => {
      const n = i + 1;
      const row = el('div', 'peek-line');
      if (source.start && n >= source.start && n <= (source.end || source.start)) {
        row.classList.add('hit');
        first ??= row;
      }
      row.append(el('span', 'ln', String(n)), el('span', 'code', line || ' '));
      text.append(row);
    });
    text.scrollTop = 0;
    if (first) text.scrollTop = Math.max(0, first.offsetTop - text.clientHeight * 0.3);
    const notes = [];
    if (source.start && source.start > lines.length) notes.push(`${answer.path} has only ${lines.length} lines.`);
    if (answer.truncated) notes.push('Only the start of this file is shown.');
    this._status(notes.join(' '), notes.length ? 'warn' : '');
  }

  _showImage(answer) {
    this._only('image');
    this.els.image.src = answer.url;
    this._status('');
  }
}

function clampPage(page, count) {
  return Math.min(Math.max(1, page), Math.max(1, count));
}

/** A PDF user-space rectangle `[x1, y1, x2, y2]` in a page's pixels. */
function userRectToPage(entry, [x1, y1, x2, y2]) {
  const [a, b, c, d] = entry.viewport.convertToViewportRectangle([x1, y1, x2, y2]);
  return {
    left: Math.min(a, c),
    top: Math.min(b, d),
    width: Math.abs(c - a),
    height: Math.abs(d - b),
  };
}
