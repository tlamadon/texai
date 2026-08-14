// Wiring: load the PDF, watch it for changes, turn Cmd/Ctrl-clicks into
// SyncTeX lookups, and keep a copyable reference to the last selection.

import { getJSON, postJSON } from './api.js';
import { ChatPanel } from './chat.js';
import { MarksLayer } from './marks.js';
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
  conn: document.getElementById('conn'),
  modKey: document.getElementById('mod-key'),
};

let marks = null;

const viewer = new PdfViewer({
  container: els.container,
  viewerEl: els.viewer,
  onPageChange: (page, count) => {
    els.pageCurrent.textContent = String(page);
    els.pageCount.textContent = String(count);
  },
  // Pages re-render on scroll and zoom, wiping anything overlaid on them.
  onRender: () => marks?.draw(),
});

const chat = new ChatPanel();
marks = new MarksLayer({ viewer, button: els.toggleMarks });
/* ---------------- moving the view ---------------- */

function showLocation(loc) {
  if (!loc || !loc.found) {
    showToast(
      `${loc?.file ?? 'That line'}:${loc?.line ?? ''} produces no output in the PDF.`,
      { type: 'error' }
    );
    return;
  }
  viewer.scrollToBox(loc.page, loc.boxes[0]);
  for (const box of loc.boxes) viewer.flashBox(box.page, box);
}

async function goTo(file, line) {
  try {
    showLocation(
      await getJSON(`/api/locate?file=${encodeURIComponent(file)}&line=${encodeURIComponent(line)}`)
    );
  } catch (err) {
    showToast(err.message || String(err), { type: 'error' });
  }
}

chat.onGoTo = goTo;
chat.onNavigate = (event) => {
  showLocation(event);
  if (event.why) showToast(event.why);
};

chat.onTurnFinished = (turn) => {
  if (turn.status === 'applied' || turn.hunks?.length) marks.showTurn(turn.id);
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
      if (isReload) showToast('PDF reloaded');
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
    const result = await postJSON('/api/select', {
      page: entry.num,
      x: Number(point.x.toFixed(3)),
      y: Number(point.y.toFixed(3)),
      selectedText: selectedText || null,
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

function onViewerClick(event) {
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
  const { file, line } = selection.source;
  let text = `PDF selection: ${file}:${line}`;
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
  try {
    const info = await getJSON('/api/info');
    els.pdfName.textContent = info.pdf;
    els.pdfName.title = `${info.root}/${info.pdf}`;
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
}

// Exposed for the browser layout suite (and handy from the devtools console).
// Read-only handles; nothing here is part of the page's own control flow.
window.__texai = { viewer, chat, marks };

boot().catch((err) => showEmpty(`Startup failed: ${err.message}`, true));
