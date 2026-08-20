// Wiring: load the PDF, watch it for changes, turn Cmd/Ctrl-clicks into
// SyncTeX lookups, and keep a copyable reference to the last selection.

import { getJSON, postJSON } from './api.js';
import { ChatPanel } from './chat.js';
import { GitPanel } from './git.js';
import { ViewHistory } from './history.js';
import { SourceEditor } from './editor.js';
import { QuickJump } from './jump.js';
import { MarksLayer, narrowRects } from './marks.js';
import { OutlinePanel } from './outline.js';
import { ProjectSources } from './project.js';
import { ScrollSync } from './sync.js';
import { showToast } from './toast.js';
import { PdfViewer } from './viewer.js';

const POLL_MS = 1000;
const IS_MAC = navigator.platform.toUpperCase().includes('MAC');

const els = {
  container: document.getElementById('viewer-container'),
  viewer: document.getElementById('viewer'),
  empty: document.getElementById('empty'),
  pdfName: document.getElementById('pdf-name'),
  pageCurrent: document.getElementById('page-current'),
  pageCount: document.getElementById('page-count'),
  zoomLevel: document.getElementById('zoom-level'),
  zoomIn: document.getElementById('zoom-in'),
  zoomOut: document.getElementById('zoom-out'),
  zoomFit: document.getElementById('zoom-fit'),
  copyRef: document.getElementById('copy-reference'),
  toggleMarks: document.getElementById('toggle-marks'),
  toggleSync: document.getElementById('toggle-sync'),
  acceptAll: document.getElementById('accept-all'),
  conn: document.getElementById('conn'),
  download: document.getElementById('download-pdf'),
  modKey: document.getElementById('mod-key'),
  composer: document.getElementById('composer'),
};

let marks = null;
let viewHistory = null;

const viewer = new PdfViewer({
  container: els.container,
  viewerEl: els.viewer,
  onPageChange: (page, count) => {
    els.pageCurrent.textContent = String(page);
    els.pageCount.textContent = String(count);
  },
  // Pages re-render on scroll and zoom, wiping anything overlaid on them.
  onRender: () => marks?.draw(),
  // Following a link moves the page; the arrow back has to know from where.
  onBeforeJump: () => viewHistory?.mark(),
});

// Back and forward over the places the document has taken you.
viewHistory = new ViewHistory({ viewer, back: 'view-back', forward: 'view-forward' });

const chat = new ChatPanel();
marks = new MarksLayer({ viewer, button: els.toggleMarks, acceptAllButton: els.acceptAll });

// Saving rebuilds the PDF; the watcher below notices the new version and
// reloads the page, so there is nothing to do here but let it happen.
const editor = new SourceEditor();
const git = new GitPanel();
const project = new ProjectSources();

// The document's contents beside the page: the root .tex with every \input
// spliced in, so it reads as one paper rather than as a pile of files. Clicking
// moves the PDF — this outline belongs to the thing being read. Alt-click opens
// the source instead, the same modifier as on the page itself.
const contents = new OutlinePanel({
  panel: 'contents',
  list: 'contents-list',
  toggle: 'contents-toggle',
  storageKey: 'texai.contents',
  empty: 'No sections found in the source.',
  // Off until asked for: the page should own the width of its pane.
  defaultVisible: false,
  onPick: (entry, event) =>
    event?.altKey
      ? editAt(entry.file, entry.line)
      : goTo(entry.file, entry.line, { phrases: entry.phrase ? [entry.phrase] : [] }),
});
// Opening the column narrows the page's half of the window, so a page that was
// fitted to the width has to be fitted again — the same thing dragging the
// splitter does.
contents.onToggle = (visible) => {
  if (visible) refreshContents();
  if (viewer.fitWidth) {
    viewer.setFitWidth();
    updateZoomReadout();
  }
};
document.getElementById('contents-search').addEventListener('click', () => jump.toggle());

// Go to a heading anywhere in the project, or to a file, without leaving the
// keyboard. The open buffer answers for itself so the line numbers are right
// even mid-edit; the rest of the project is read from disk and cached.
const jump = new QuickJump({
  project,
  current: () => ({ file: editor.file, entries: editor.entries }),
  onPick: (file, line, phrase) => goToPlace(file, line, phrase),
  onDismiss: () => editor.focus(),
});
editor.onJump = () => jump.toggle();

editor.onSaved = () => {
  git.refresh();
  invalidateOutlines();
};
// The reverse of Cmd-click: put the cursor in the source, see it in the PDF.
editor.onReveal = (file, line, phrases = []) =>
  file && goTo(file, line, { quiet: true, phrases });

// Scrolling one pane scrolls the other, when asked for. Created after the
// editor, whose scroll events it listens to.
const scrollSync = new ScrollSync({ viewer, editor, button: els.toggleSync });

// The composer writes to the agent, so it only belongs to the chat views.
chat.onViewChange = (view) => {
  els.composer.hidden = view === 'source';
  editor.setActive(view === 'source');
  // Coming back to a source that has been sitting still while the page moved:
  // catch it up rather than wait for the next scroll.
  if (view === 'source') scrollSync.align();
};

/** Open the source behind a spot in the document. */
async function editAt(file, line, column = null) {
  chat.setView('source');
  await editor.open(file, line, column);
}

/**
 * Go to a place the palette named.
 *
 * A heading is a place in the document, so the page moves — that is what you
 * are reading, and being thrown into the editor to reach it is backwards. The
 * editor follows only when it is the visible tab, so a jump never changes what
 * the right-hand pane is showing. A file is a place in the source alone, and a
 * line that produced no output in the PDF has nowhere else to go.
 */
async function goToPlace(file, line, phrase = null) {
  if (line == null) {
    await editAt(file);
    return;
  }
  const found = await goTo(file, line, { quiet: true, phrases: phrase ? [phrase] : [] });
  if (found?.found) {
    if (chat.view === 'source') editor.open(file, line);
    return;
  }
  await editAt(file, line);
  // `null` means the lookup itself failed or was superseded; only an answered
  // "nothing there" is worth explaining.
  if (found) showToast(`${file}:${line} produces no output in the PDF — opened the source.`);
}

/** The document's contents, rebuilt from the source on disk. */
let contentsToken = 0;
async function refreshContents() {
  if (!contents.visible) return;
  const token = ++contentsToken;
  try {
    const entries = await project.document();
    if (token === contentsToken) contents.setEntries(entries);
  } catch {
    /* the source may be unreadable; the panel keeps what it had */
  }
}

/** Someone wrote to the project: every cached outline is now a guess. */
function invalidateOutlines() {
  project.invalidate();
  jump.invalidate();
  refreshContents();
}
/* ---------------- moving the view ---------------- */

/** Wait for a page's text layer, which word-narrowing reads. */
function textLayerReady(page, timeout = 800) {
  return new Promise((resolve) => {
    const deadline = performance.now() + timeout;
    const tick = () => {
      if (viewer.hasTextLayer(page) || performance.now() > deadline) resolve();
      else requestAnimationFrame(tick);
    };
    tick();
  });
}

/**
 * The rectangles for a phrase inside the line SyncTeX reported.
 *
 * Candidates are tried longest first: a five-word run is unmistakable when it
 * is there, and gone entirely when the line broke or a ligature swallowed it,
 * in which case a shorter one still lands somewhere sensible.
 */
async function preciseRects(loc, phrases) {
  // One wait, for the page just scrolled to. Other pages are only worth trying
  // if they happen to be rendered already — a line's boxes rarely straddle two.
  await textLayerReady(loc.page);
  for (const box of loc.boxes) {
    const entry = viewer.pageEntry(box.page);
    if (!entry?.viewport || !viewer.hasTextLayer(box.page)) continue;
    const band = viewer.pdfRectToPageRect(entry, box);
    for (const phrase of phrases) {
      const rects = narrowRects(entry, band, [phrase]);
      if (rects.length) return { entry, rects };
    }
  }
  return null;
}

// Only the newest reveal may flash; narrowing waits on a render, and a stale
// answer landing afterwards would highlight where you were, not where you are.
let showToken = 0;

async function showLocation(loc, { phrases = [] } = {}) {
  const token = ++showToken;
  if (!loc || !loc.found) {
    showToast(
      `${loc?.file ?? 'That line'}:${loc?.line ?? ''} produces no output in the PDF.`,
      { type: 'error' }
    );
    return;
  }
  // Scroll first: narrowing reads the text layer, which only exists for pages
  // near the view, so the page has to be brought there before it can be read.
  viewHistory.mark();
  viewer.scrollToBox(loc.page, loc.boxes[0]);

  const precise = phrases.length ? await preciseRects(loc, phrases) : null;
  if (token !== showToken) return;

  if (precise) {
    // Centre on the words themselves — they may be on the second rendered line
    // of a source line that wrapped.
    viewer.scrollToRect(precise.entry, precise.rects[0]);
    for (const rect of precise.rects) viewer.flashRect(precise.entry, rect);
    return;
  }
  // Nothing to narrow to, or the words are not on the page as written: the
  // whole line is still the honest answer.
  for (const box of loc.boxes) viewer.flashBox(box.page, box);
}

// Rapid clicks in the editor each start a lookup; only the newest may move the
// view, or an earlier answer would land after a later one and scroll it back.
let revealToken = 0;

async function goTo(file, line, { quiet = false, phrases = [] } = {}) {
  const token = ++revealToken;
  try {
    const found = await getJSON(
      `/api/locate?file=${encodeURIComponent(file)}&line=${encodeURIComponent(line)}`
    );
    if (token !== revealToken) return null;
    // Plenty of source lines produce no output — comments, \begin{document},
    // a macro definition. When the user merely clicked in the editor that is
    // not worth an error; it just means there is nothing to show.
    if (quiet && !found.found) return found;
    showLocation(found, { phrases });
    return found;
  } catch (err) {
    if (token !== revealToken || quiet) return null;
    showToast(err.message || String(err), { type: 'error' });
    return null;
  }
}

chat.onGoTo = goTo;
marks.onEdit = (file, line) => editAt(file, line);
// A marked passage carries a column when the word was pinned down.
chat.onEdit = (file, line, column, word) => editAt(file, line, word ? column : null);
chat.onNavigate = (event) => {
  showLocation(event);
  if (event.why) showToast(event.why);
};

// Changes accumulate across turns until accepted, so any of these just means
// "the pending set moved".
chat.onChangesUpdated = () => {
  marks.refresh();
  git.refresh();
  // The agent's edits move headings around; the cached outlines are stale.
  invalidateOutlines();
  // The agent may have rewritten the file sitting in the editor; fold its
  // version into the buffer, keeping any unsaved typing that is out of its way.
  editor.reload();
};

let lastSelection = null;
let pdfVersion = null;
let busy = false;

/* ---------------- loading & watching ---------------- */

function showEmpty(message, isError = false) {
  els.empty.textContent = message;
  els.empty.classList.toggle('error', isError);
  els.empty.style.display = message ? '' : 'none';
}

async function loadPdf(version, { preserveView = false } = {}) {
  const state = preserveView ? viewer.getViewState() : null;
  // The version tag busts both the browser cache and any stale worker copy.
  const url = `/api/pdf?v=${encodeURIComponent(version ?? 'initial')}`;
  await viewer.load(url);
  if (state) viewer.applyViewState(state);
  pdfVersion = version;
  els.download.href = url;
  showEmpty('');
  marks?.refresh();
  updateZoomReadout();
  els.pageCount.textContent = String(viewer.pageCount);
}

let polling = false;

async function poll() {
  if (polling) return; // a reload may outlive one tick
  polling = true;
  try {
    const status = await getJSON('/api/status');
    els.conn.textContent = 'watching';
    els.conn.classList.remove('stale');
    if (!status.exists) {
      showEmpty('PDF is missing — waiting for it to be rebuilt…', true);
      return;
    }
    if (status.pdfVersion !== pdfVersion) {
      const isReload = pdfVersion !== null;
      await loadPdf(status.pdfVersion, { preserveView: isReload });
      if (isReload) {
        showToast('PDF reloaded');
        // A rebuild we did not start — latexmk in a terminal, say, or an agent
        // outside this app. The source behind this PDF has moved on, and so
        // has its outline; the editor takes what it can of the new text.
        invalidateOutlines();
        editor.reload();
        // The lines moved under the mapping; ask it again for this spot.
        scrollSync.align();
      }
    }
  } catch (err) {
    els.conn.textContent = 'disconnected';
    els.conn.classList.add('stale');
  } finally {
    polling = false;
  }
}

/* ---------------- selection ---------------- */

async function select(entry, clientX, clientY, selectedText) {
  if (busy) return;
  busy = true;
  try {
    const point = viewer.clientPointToPdf(entry, clientX, clientY);
    // SyncTeX only answers with a line. The word under the cursor, and the
    // words either side of it, are what get the server the rest of the way.
    const word = viewer.wordAtClientPoint(clientX, clientY);
    const result = await postJSON('/api/select', {
      page: entry.num,
      x: Number(point.x.toFixed(3)),
      y: Number(point.y.toFixed(3)),
      selectedText: selectedText || null,
      word: word?.word ?? null,
      contextBefore: word?.before ?? null,
      contextAfter: word?.after ?? null,
    });
    lastSelection = result.selection;
    els.copyRef.disabled = false;
    // The passage becomes a chip on the composer; the selection file is still
    // written for agents driving texai from outside the browser.
    chat.addSelection(result.selection);
    showToast(result.message);
  } catch (err) {
    showToast(err.message || String(err), { type: 'error' });
  } finally {
    busy = false;
  }
}

/** Alt-click: jump to the source behind the click, without recording it. */
async function openSourceAt(entry, clientX, clientY) {
  try {
    const point = viewer.clientPointToPdf(entry, clientX, clientY);
    const word = viewer.wordAtClientPoint(clientX, clientY);
    const where = await postJSON('/api/resolve', {
      page: entry.num,
      x: Number(point.x.toFixed(3)),
      y: Number(point.y.toFixed(3)),
      selectedText: null,
      word: word?.word ?? null,
      contextBefore: word?.before ?? null,
      contextAfter: word?.after ?? null,
    });
    // Land on the word itself, not the start of its line. When that could not
    // be worked out, say so — a cursor at column 1 with no explanation reads
    // like the feature is broken rather than declining to guess.
    await editAt(where.file, where.line, where.word ? where.column : null);
    if (!where.word && where.why) {
      showToast(`Opened ${where.file}:${where.line} — ${where.why}`, { type: 'error' });
    }
  } catch (err) {
    showToast(err.message || String(err), { type: 'error' });
  }
}

function onViewerClick(event) {
  if (event.altKey && event.button === 0) {
    event.preventDefault();
    const entry = viewer.pageAtClientPoint(event.clientX, event.clientY);
    if (entry) openSourceAt(entry, event.clientX, event.clientY);
    return;
  }

  const modifier = IS_MAC ? event.metaKey : event.ctrlKey || event.metaKey;
  if (!modifier || event.button !== 0) return;
  event.preventDefault();

  // A live text selection wins: use its midpoint and ship the rendered text.
  const selection = viewer.getSelectionInfo();
  if (selection) {
    select(selection.entry, selection.clientX, selection.clientY, selection.text);
    return;
  }

  const entry = viewer.pageAtClientPoint(event.clientX, event.clientY);
  if (!entry) return;
  select(entry, event.clientX, event.clientY, null);
}

function referenceText(selection) {
  const { file, line, column, word } = selection.source;
  let text = `PDF selection: ${file}:${line}${word ? `:${column}` : ''}`;
  if (word) text += `\nAt the word: “${word}”`;
  if (selection.selectedText) {
    text += `\nRendered text: “${selection.selectedText}”`;
  }
  return text;
}

async function copyReference() {
  if (!lastSelection) return;
  const text = referenceText(lastSelection);
  try {
    await navigator.clipboard.writeText(text);
    showToast('Reference copied');
  } catch {
    // Clipboard API can be blocked; fall back to a hidden textarea.
    const area = document.createElement('textarea');
    area.value = text;
    area.style.position = 'fixed';
    area.style.opacity = '0';
    document.body.append(area);
    area.select();
    const ok = document.execCommand('copy');
    area.remove();
    showToast(ok ? 'Reference copied' : `Copy failed — reference:\n${text}`, {
      type: ok ? 'info' : 'error',
    });
  }
}

/* ---------------- ui wiring ---------------- */

function updateZoomReadout() {
  els.zoomLevel.textContent = `${Math.round(viewer.scale * 100)}%`;
}

els.zoomIn.addEventListener('click', () => {
  viewer.zoomIn();
  updateZoomReadout();
});
els.zoomOut.addEventListener('click', () => {
  viewer.zoomOut();
  updateZoomReadout();
});
els.zoomFit.addEventListener('click', () => {
  viewer.setFitWidth();
  updateZoomReadout();
});
els.copyRef.addEventListener('click', copyReference);
els.viewer.addEventListener('click', onViewerClick);

// Drag the divider between the document and the chat panel.
(() => {
  const splitter = document.getElementById('splitter');
  if (!splitter) return;
  let dragging = false;

  const apply = (clientX) => {
    const width = Math.min(Math.max(window.innerWidth - clientX, 280), window.innerWidth - 320);
    document.documentElement.style.setProperty('--chat-width', `${Math.round(width)}px`);
  };

  splitter.addEventListener('mousedown', (event) => {
    dragging = true;
    event.preventDefault();
    document.body.style.cursor = 'col-resize';
  });
  window.addEventListener('mousemove', (event) => dragging && apply(event.clientX));
  window.addEventListener('mouseup', () => {
    if (!dragging) return;
    dragging = false;
    document.body.style.cursor = '';
    if (viewer.fitWidth) viewer.setFitWidth();
    updateZoomReadout();
  });
})();

// Cmd/Ctrl-P opens the go-to palette from anywhere, including from inside the
// editor and the composer — so it is caught on the way down, before CodeMirror
// or the browser's print dialog get a say. Ctrl-P on a Mac is left alone: it
// moves the cursor up a line, as it does in every other text field there.
document.addEventListener(
  'keydown',
  (event) => {
    const modifier = IS_MAC ? event.metaKey && !event.ctrlKey : event.ctrlKey && !event.metaKey;
    if (!modifier || event.altKey || event.shiftKey) return;
    if (event.key !== 'p' && event.key !== 'P') return;
    event.preventDefault();
    event.stopPropagation();
    jump.toggle();
  },
  true
);

document.addEventListener('keydown', (event) => {
  if (event.target instanceof HTMLInputElement) return;
  if (event.target instanceof HTMLTextAreaElement) return;
  if ((event.metaKey || event.ctrlKey) && (event.key === '=' || event.key === '+')) {
    event.preventDefault();
    viewer.zoomIn();
    updateZoomReadout();
  } else if ((event.metaKey || event.ctrlKey) && event.key === '-') {
    event.preventDefault();
    viewer.zoomOut();
    updateZoomReadout();
  }
});

/* ---------------- boot ---------------- */

async function boot() {
  els.modKey.textContent = IS_MAC ? 'Cmd' : 'Ctrl';
  const shortcut = `(${IS_MAC ? 'Cmd' : 'Ctrl'}-P)`;
  for (const id of ['outline-search', 'contents-search']) {
    const button = document.getElementById(id);
    button.title = `${button.title} ${shortcut}`;
  }
  refreshContents();
  try {
    const info = await getJSON('/api/info');
    els.pdfName.textContent = info.pdf;
    els.pdfName.title = `${info.root}/${info.pdf}`;
    els.download.download = info.pdf.split('/').pop();
    els.download.title = `Download ${els.download.download}`;
    // Where the editor was last left, remembered per project rather than per
    // browser: two papers open on the same port are two different places.
    editor.setPlaceScope(info.root);
    document.title = `${info.pdf} — texai`;
  } catch (err) {
    showEmpty(`Cannot reach the texai server: ${err.message}`, true);
    return;
  }

  try {
    const previous = await getJSON('/api/selection');
    lastSelection = previous;
    els.copyRef.disabled = false;
  } catch {
    /* no selection recorded yet */
  }

  await poll();
  setInterval(poll, POLL_MS);
  chat.start().catch(() => showToast('Chat panel failed to start', { type: 'error' }));
  git.start().catch(() => {
    /* a project outside git simply has no pill */
  });
}

// Exposed for the browser layout suite (and handy from the devtools console).
// Read-only handles; nothing here is part of the page's own control flow.
window.__texai = {
  viewer, chat, marks, editor, git, jump, contents, project, refreshContents,
  history: viewHistory, scrollSync,
};

boot().catch((err) => showEmpty(`Startup failed: ${err.message}`, true));
