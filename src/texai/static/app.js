// Wiring: load the PDF, watch it for changes, turn Cmd/Ctrl-clicks into
// SyncTeX lookups, double-clicks into in-place edits, and keep a copyable
// reference to the last selection.

import { getJSON, postJSON } from './api.js';
import { PageAsk } from './ask.js';
import { ChatPanel } from './chat.js';
import { CommentBox } from './comments.js';
import { GitPanel } from './git.js';
import { InlineEditor } from './inline.js';
import { ViewHistory } from './history.js';
import { SourceEditor } from './editor.js';
import { QuickJump } from './jump.js';
import { MarksLayer, narrowRects } from './marks.js';
import { OutlinePanel } from './outline.js';
import { ParagraphGuide } from './paraguide.js';
import { AnnotationLayer } from './annotations.js';
import { PeekPanel } from './peek.js';
import { ProjectSources } from './project.js';
import { ScrollSync } from './sync.js';
import { SlideBlocks } from './slides.js';
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
  toggleNotes: document.getElementById('toggle-notes'),
  toggleSources: document.getElementById('toggle-sources'),
  toggleGuides: document.getElementById('toggle-guides'),
  toggleSync: document.getElementById('toggle-sync'),
  acceptAll: document.getElementById('accept-all'),
  conn: document.getElementById('conn'),
  download: document.getElementById('download-pdf'),
  modKey: document.getElementById('mod-key'),
  composer: document.getElementById('composer'),
  app: document.querySelector('.app'),
  panelToggle: document.getElementById('panel-toggle'),
  viewButton: document.getElementById('view-button'),
  viewMenu: document.getElementById('view-menu'),
  hintbar: document.getElementById('hintbar'),
  toggleHints: document.getElementById('toggle-hints'),
  hintsDismiss: document.getElementById('hints-dismiss'),
};

let marks = null;
let paraGuide = null;
let slides = null;
let notes = null;
let viewHistory = null;
let inline = null;
let ask = null;
let comments = null;

const viewer = new PdfViewer({
  container: els.container,
  viewerEl: els.viewer,
  onPageChange: (page, count) => {
    els.pageCurrent.textContent = String(page);
    els.pageCount.textContent = String(count);
  },
  // Pages re-render on scroll and zoom, wiping anything overlaid on them.
  onRender: () => {
    marks?.draw();
    paraGuide?.draw();
    slides?.draw();
    notes?.draw();
    inline?.place();
    ask?.place();
    comments?.place();
  },
  // Following a link moves the page; the arrow back has to know from where.
  onBeforeJump: () => viewHistory?.mark(),
});

// Back and forward over the places the document has taken you.
viewHistory = new ViewHistory({ viewer, back: 'view-back', forward: 'view-forward' });

const chat = new ChatPanel();
marks = new MarksLayer({ viewer, button: els.toggleMarks, acceptAllButton: els.acceptAll });
// The page-side twin of the editor's paragraph gutter. Refreshed whenever the
// PDF is (re)built; redrawn on every render alongside the change markers. Its
// answer also places the author's `%@` annotations, drawn in the margins.
notes = new AnnotationLayer({
  viewer,
  button: els.toggleNotes,
  sourcesButton: els.toggleSources,
});
paraGuide = new ParagraphGuide({
  viewer,
  button: els.toggleGuides,
  onAnnotations: (items) => notes.set(items),
  skip: (page) => !!slides?.frameOn(page),
});

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

// Double-click a word on the page to edit its paragraph right there. The box
// reads and writes the file on disk, so a buffer in the Source tab with unsaved
// edits to the same file would be talking past it — that is the one refusal.
inline = new InlineEditor({
  viewer,
  container: els.container,
  resolve: (entry, clientX, clientY) => resolvePoint(entry, clientX, clientY),
  bars: () => paraGuide.bars,
  refuse: (file) =>
    editor.dirty && editor.file === file
      ? `${file} has unsaved edits in the Source tab — save or revert them first.`
      : null,
  onSaved: (result) => {
    if (!result.changed) return;
    git.refresh();
    invalidateOutlines();
    // The Source tab may have this file open; fold the new paragraph in.
    editor.reload();
  },
  onError: (message) => showToast(message, { type: 'error' }),
});

// Select a passage and ask for a change right there: a button at the end of the
// selection opens a request box, the request goes to the agent as a turn, and
// its edits come back as markers on the page — no need for the chat panel.
// Comments, written on a selection or answered from the margin. Saved as the
// reader's own edits, so they wait only for nothing else to be editing.
comments = new CommentBox({ viewer });
comments.onEdit = (file, line) => editAt(file, line);
notes.onThread = (item, items, anchor) => comments.thread(item, items, anchor);

ask = new PageAsk({
  viewer,
  container: els.container,
  select: (passage) => recordSelection(passage),
  available: () => chat.agent.available && !chat.busy,
  onComment: (passage) => comments.compose(onSlide(passage)),
  onEdit: (passage) => inline.openBlock(passage.source, passage.page, passage.source.rects),
  canComment: () => !chat.busy,
  onChanges: () => marks.reveal(),
  onOpenChat: () => {
    setPanelVisible(true);
    chat.setView('chat');
  },
});
// On a beamer deck SyncTeX knows the slide and nothing finer, so each slide
// wears an icon per block — title, item, paragraph — placed from the source.
// Clicking one offers the same two buttons a selection does; with Cmd/Ctrl it
// goes to the composer as a passage, with Alt to the editor.
slides = new SlideBlocks({
  viewer,
  anchorsButton: document.getElementById('toggle-anchors'),
  pickButton: document.getElementById('pick-block'),
  onPick: (passage, event) => {
    const { source } = passage;
    if (event.altKey) {
      editAt(source.file, source.line);
      return;
    }
    if (IS_MAC ? event.metaKey : event.ctrlKey || event.metaKey) {
      addSlideSelection(source, passage.page, passage.text);
      return;
    }
    if (!ask.offerPassage(passage)) showToast('The agent is busy; try again in a moment.');
  },
});
notes.slideSpots = (file, line) => slides.spotsFor(file, line);
// A note sits just past its block's anchor, so it moves when the anchors go.
document.getElementById('toggle-anchors')?.addEventListener('click', () => notes.draw());
chat.onTurnFinished = (turn) => ask.turnFinished(turn);
chat.onAgentEntry = (turnId, entry) => ask.agentEntry(turnId, entry);
chat.onBuild = (event) => ask.buildEvent(event);

// Scrolling one pane scrolls the other, when asked for. Created after the
// editor, whose scroll events it listens to.
const scrollSync = new ScrollSync({ viewer, editor, button: els.toggleSync });

// The composer writes to the agent, so it only belongs to the chat views.
chat.onViewChange = (view) => {
  els.composer.hidden = view === 'source' || view === 'peek';
  editor.setActive(view === 'source');
  // Coming back to a source that has been sitting still while the page moved:
  // catch it up rather than wait for the next scroll.
  if (view === 'source') scrollSync.align();
};

/* ---------------- the right-hand panel ---------------- */

// The chat and the source can be put away, leaving the page the whole window;
// with editing in place on the page, reading and fixing need nothing else. The
// choice is remembered. Anything that only makes sense in the panel — opening a
// source, attaching a passage — brings it back rather than working unseen.
const PANEL_KEY = 'texai.panel';
let panelVisible = true;

function setPanelVisible(visible) {
  if (visible === panelVisible) return;
  panelVisible = visible;
  els.app.classList.toggle('panel-hidden', !visible);
  els.panelToggle.classList.toggle('on', visible);
  els.panelToggle.setAttribute('aria-pressed', String(visible));
  els.panelToggle.title = `${visible ? 'Hide' : 'Show'} the chat and source panel ${panelShortcut()}`;
  try {
    localStorage.setItem(PANEL_KEY, visible ? '1' : '0');
  } catch {
    /* storage disabled: the panel just will not be remembered */
  }
  // The page's half of the window changed size, as dragging the splitter does.
  if (viewer.fitWidth) {
    viewer.setFitWidth();
    updateZoomReadout();
  }
  // CodeMirror measures nothing while hidden, and the page may have moved on.
  if (visible && chat.view === 'source') {
    setTimeout(() => {
      editor.cm?.refresh();
      scrollSync.align();
    }, 0);
  }
}

function panelShortcut() {
  return `(${IS_MAC ? 'Cmd' : 'Ctrl'}-\\)`;
}

els.panelToggle.addEventListener('click', () => setPanelVisible(!panelVisible));

/** Open the source behind a spot in the document. */
async function editAt(file, line, column = null) {
  setPanelVisible(true);
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
notes.onEdit = (file, line) => editAt(file, line);

// What backs a sentence, opened in the Peek tab beside the page.
const peek = new PeekPanel();
peek.pdfUrl = () => `/api/pdf?v=${encodeURIComponent(pdfVersion ?? 'initial')}`;
peek.onEdit = (file, line) => editAt(file, line);
peek.onShowInPaper = (location) => showLocation(location);
notes.onPeek = (item) => {
  setPanelVisible(true);
  chat.setView('peek');
  peek.show(item);
};
// A marked passage carries a column when the word was pinned down.
chat.onEdit = (file, line, column, word) => editAt(file, line, word ? column : null);
chat.onNavigate = (event) => {
  showLocation(event);
  if (event.why) showToast(event.why);
};

// Inline sources and comments, moved into annotations.jsonl in one go. The
// source loses their lines, so it is saved over what the editor holds only
// with nothing unsaved there, and the paper is rebuilt.
editor.onExternalize = async () => {
  if (editor.dirty) {
    showToast('Save or discard your edits first.', { type: 'error' });
    return;
  }
  if (!window.confirm('Move every %@src and comment into annotations.jsonl, leaving a marker on each sentence?')) return;
  try {
    const done = await postJSON('/api/annotations/externalize', {});
    if (!done.moved) {
      showToast('No inline sources or comments to move.');
      return;
    }
    editor.reload({ force: true });
    showToast(`Moved ${done.moved} annotation${done.moved > 1 ? 's' : ''} from ${done.files.join(', ')}.`);
  } catch (err) {
    showToast(err.message || String(err), { type: 'error' });
  }
};

// A comment kept in annotations.jsonl changed without a rebuild: read the
// margins again, and take the marker into the editor if it was added there.
chat.onAnnotationsChanged = () => {
  paraGuide.refresh();
  editor.reload();
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
  paraGuide?.refresh();
  // The bars stay off a slide, so they are drawn again once the slides are known.
  slides?.refresh().then(() => {
    paraGuide?.draw();
    notes?.draw(); // on a slide a note sits by its block, once the blocks are known
  });
  inline?.pdfReloaded();
  ask?.pdfReloaded();
  updateZoomReadout();
  els.pageCount.textContent = String(viewer.pageCount);
}

let polling = false;

// A quiet dot while the server answers; words only when it has stopped.
function setConnected(connected) {
  els.conn.classList.toggle('stale', !connected);
  els.conn.textContent = connected ? '' : 'disconnected';
  els.conn.title = connected
    ? 'Watching the PDF for rebuilds'
    : 'The texai server is not answering — is it still running?';
}

async function poll() {
  if (polling) return; // a reload may outlive one tick
  polling = true;
  try {
    const status = await getJSON('/api/status');
    setConnected(true);
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
    setConnected(false);
  } finally {
    polling = false;
  }
}

/* ---------------- selection ---------------- */

/**
 * A passage on a slide, with the block of the source it belongs to. A passage
 * anywhere else comes back as it is, to be resolved through SyncTeX.
 */
function onSlide(passage) {
  if (passage.source) return passage;
  const source = slides?.sourceAt(passage.page, passage.point);
  return source ? { ...passage, source } : passage;
}

/** Put a block of a slide on the composer, as a Cmd-click puts a word there. */
function addSlideSelection(source, page, selectedText) {
  lastSelection = { source, page, selectedText: selectedText || null };
  els.copyRef.disabled = false;
  setPanelVisible(true);
  chat.addSelection(lastSelection);
  const lines = source.endLine > source.line ? `${source.line}-${source.endLine}` : source.line;
  showToast(`Selected ${source.file}:${lines}`);
}

/** Resolve a point on a page to its source, and record it as the current selection. */
async function recordSelection({ page, point, word, selectedText }) {
  const source = slides?.sourceAt(page, point);
  if (source) {
    lastSelection = { source, page, selectedText: selectedText || source.text || null };
    els.copyRef.disabled = false;
    return { selection: lastSelection, message: `Selected ${source.file}:${source.line}` };
  }
  const result = await postJSON('/api/select', {
    page,
    x: Number(point.x.toFixed(3)),
    y: Number(point.y.toFixed(3)),
    selectedText: selectedText || null,
    word: word?.word ?? null,
    contextBefore: word?.before ?? null,
    contextAfter: word?.after ?? null,
  });
  lastSelection = result.selection;
  els.copyRef.disabled = false;
  return result;
}

async function select(entry, clientX, clientY, selectedText) {
  if (busy) return;
  busy = true;
  try {
    // SyncTeX only answers with a line. The word under the cursor, and the
    // words either side of it, are what get the server the rest of the way.
    const result = await recordSelection({
      page: entry.num,
      point: viewer.clientPointToPdf(entry, clientX, clientY),
      word: viewer.wordAtClientPoint(clientX, clientY),
      selectedText,
    });
    // The passage lands on the composer, so the composer has to be there.
    setPanelVisible(true);
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

/** Where in the source a point on the page came from, without recording it. */
function resolvePoint(entry, clientX, clientY) {
  const point = viewer.clientPointToPdf(entry, clientX, clientY);
  const source = slides?.sourceAt(entry.num, point);
  if (source) return Promise.resolve({ ...source, why: '' });
  const word = viewer.wordAtClientPoint(clientX, clientY);
  return postJSON('/api/resolve', {
    page: entry.num,
    x: Number(point.x.toFixed(3)),
    y: Number(point.y.toFixed(3)),
    selectedText: null,
    word: word?.word ?? null,
    contextBefore: word?.before ?? null,
    contextAfter: word?.after ?? null,
  });
}

/** Alt-click: jump to the source behind the click, without recording it. */
async function openSourceAt(entry, clientX, clientY) {
  try {
    const where = await resolvePoint(entry, clientX, clientY);
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

/** Double-click: edit the paragraph under the pointer, on the page. */
function onViewerDoubleClick(event) {
  if (event.button !== 0 || event.altKey || event.metaKey || event.ctrlKey || event.shiftKey) return;
  // Links, margin notes and change markers have double-clicks of their own to lose.
  if (event.target.closest?.('a, button')) return;
  const entry = viewer.pageAtClientPoint(event.clientX, event.clientY);
  if (!entry) return;
  // The browser has just selected the word; the box selects it instead.
  const word = viewer.wordAtClientPoint(event.clientX, event.clientY);
  window.getSelection()?.removeAllRanges();
  // On a slide the box opens on the block under the pointer (the source says
  // what its lines are), or on the whole frame when the pointer is on none.
  const block = slides?.sourceAt(entry.num, viewer.clientPointToPdf(entry, event.clientX, event.clientY));
  if (block) {
    ask.dismissOffer();
    // The word that was double-clicked is selected in the box, as it was on
    // the page a moment ago.
    inline.openBlock(block, entry.num, block.rects, word);
    return;
  }
  inline.open(entry, event.clientX, event.clientY, event.target);
}

function referenceText(selection) {
  const { file, line, column, word, endLine } = selection.source;
  const range = endLine > line ? `-${endLine}` : word ? `:${column}` : '';
  let text = `PDF selection: ${file}:${line}${range}`;
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
els.copyRef.addEventListener('click', () => {
  copyReference();
  setViewMenu(false);
});

/* ---------------- the View menu and the hint line ---------------- */

// What is drawn over the page is chosen once and then left alone, so it lives
// behind one button rather than as a row of them. The menu stays open while
// its boxes are ticked — several are often changed together.
function setViewMenu(open) {
  els.viewMenu.hidden = !open;
  els.viewButton.setAttribute('aria-expanded', String(open));
  els.viewButton.classList.toggle('on', open);
}
els.viewButton.addEventListener('click', () => setViewMenu(els.viewMenu.hidden));
document.addEventListener('click', (event) => {
  if (els.viewMenu.hidden) return;
  if (els.viewButton.contains(event.target) || els.viewMenu.contains(event.target)) return;
  setViewMenu(false);
});
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && !els.viewMenu.hidden) setViewMenu(false);
});

// The mouse shortcuts under the page: worth a line until they are known, then
// worth the line back.
const HINTS_KEY = 'texai.hints';
function setHints(visible, { remember = true } = {}) {
  els.hintbar.hidden = !visible;
  els.toggleHints.setAttribute('aria-pressed', String(visible));
  if (!remember) return;
  try {
    localStorage.setItem(HINTS_KEY, visible ? '1' : '0');
  } catch {
    /* storage disabled: the hints just come back next time */
  }
}
els.toggleHints.addEventListener('click', () => setHints(els.hintbar.hidden));
els.hintsDismiss.addEventListener('click', () => setHints(false));
try {
  if (localStorage.getItem(HINTS_KEY) === '0') setHints(false, { remember: false });
} catch {
  /* storage disabled */
}
els.viewer.addEventListener('click', onViewerClick);
els.viewer.addEventListener('dblclick', onViewerDoubleClick);

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
// Cmd/Ctrl-\ puts the right-hand panel away or brings it back, from anywhere.
document.addEventListener(
  'keydown',
  (event) => {
    const modifier = IS_MAC ? event.metaKey && !event.ctrlKey : event.ctrlKey && !event.metaKey;
    if (!modifier || event.altKey || event.shiftKey) return;
    if (event.key === '\\') {
      event.preventDefault();
      event.stopPropagation();
      setPanelVisible(!panelVisible);
      return;
    }
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

/* ---------------- a newer texai ---------------- */

// The server asks PyPI, at most twice a day; this only shows what it heard. An
// arrow beside the name when there is a newer release, and the command that
// fetches it on a click — texai does not update itself under a running paper.
const UPDATE_POLL_MS = 6 * 3600 * 1000;
let update = null;

async function checkForUpdate() {
  try {
    update = await getJSON('/api/update');
  } catch {
    return; // an older server, or none: nothing to show
  }
  const pill = document.getElementById('update-pill');
  pill.hidden = !update.available;
  if (update.available) {
    pill.title = `texai ${update.latest} is out — you have ${update.current}. Click for how to update.`;
  }
}

document.getElementById('update-pill').addEventListener('click', async () => {
  if (!update?.available) return;
  let copied = false;
  try {
    await navigator.clipboard.writeText(update.command);
    copied = true;
  } catch {
    /* clipboard blocked: the command is in the toast to copy by hand */
  }
  showToast(
    `texai ${update.latest} is out (you have ${update.current}).\n` +
      `Stop texai, then run:  ${update.command}${copied ? '\n(copied)' : ''}`
  );
});

/* ---------------- boot ---------------- */

async function boot() {
  els.modKey.textContent = IS_MAC ? 'Cmd' : 'Ctrl';
  const shortcut = `(${IS_MAC ? 'Cmd' : 'Ctrl'}-P)`;
  for (const id of ['outline-search', 'contents-search']) {
    const button = document.getElementById(id);
    button.title = `${button.title} ${shortcut}`;
  }
  els.panelToggle.title = `${els.panelToggle.title} ${panelShortcut()}`;
  // Before the PDF loads, so it is fitted to the width it will actually have.
  try {
    if (localStorage.getItem(PANEL_KEY) === '0') setPanelVisible(false);
  } catch {
    /* storage disabled: start with the panel showing */
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
    editor.configureCompletion(info.completion || { available: false });
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
  checkForUpdate();
  setInterval(checkForUpdate, UPDATE_POLL_MS);
  chat.start().catch(() => showToast('Chat panel failed to start', { type: 'error' }));
  git.start().catch(() => {
    /* a project outside git simply has no pill */
  });
}

// Exposed for the browser layout suite (and handy from the devtools console).
// Read-only handles; nothing here is part of the page's own control flow.
window.__texai = {
  viewer, chat, marks, paraGuide, notes, editor, git, jump, contents, project, refreshContents,
  history: viewHistory, scrollSync, inline, ask, setPanelVisible,
};

boot().catch((err) => showEmpty(`Startup failed: ${err.message}`, true));
