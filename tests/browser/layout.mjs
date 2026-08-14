// Layout regression checks, driven over the Chrome DevTools Protocol.
//
// These exist because every layout bug this project has hit was invisible to
// the Python tests and to DOM-property assertions alike: a flex child without
// min-height:0 that pushed the page taller than the viewport, a `hidden`
// attribute outranked by `display:flex`, and turn cards squashing to slivers
// instead of scrolling. All three only show up in measured geometry.
//
// Run with tests/browser/run.sh — it starts texai and headless Chrome.

const PORT = Number(process.env.TEXAI_PORT || 8795);
const DEBUG_PORT = Number(process.env.CHROME_DEBUG_PORT || 9223);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const results = [];
const check = (name, ok, detail = '') => {
  results.push({ name, ok });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? ` — ${detail}` : ''}`);
};

async function findPageTarget() {
  for (let i = 0; i < 60; i += 1) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${DEBUG_PORT}/json/list`)).json();
      const page = list.find((t) => t.type === 'page' && t.url.includes(String(PORT)));
      if (page?.webSocketDebuggerUrl) return page;
    } catch {}
    await sleep(250);
  }
  throw new Error('no page target found — is Chrome running with --remote-debugging-port?');
}

class CDP {
  constructor(ws) {
    this.ws = ws;
    this.id = 0;
    this.pending = new Map();
    ws.addEventListener('message', (ev) => {
      const msg = JSON.parse(ev.data);
      const entry = this.pending.get(msg.id);
      if (!entry) return;
      this.pending.delete(msg.id);
      msg.error ? entry.reject(new Error(JSON.stringify(msg.error))) : entry.resolve(msg.result);
    });
  }
  send(method, params = {}) {
    const id = ++this.id;
    this.ws.send(JSON.stringify({ id, method, params }));
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      setTimeout(() => reject(new Error(`${method} timed out`)), 30000);
    });
  }
  async eval(expression) {
    const r = await this.send('Runtime.evaluate', {
      expression,
      awaitPromise: true,
      returnByValue: true,
    });
    if (r.exceptionDetails) throw new Error(r.exceptionDetails.text);
    return r.result.value;
  }
  json(expression) {
    return this.eval(`JSON.stringify(${expression})`).then(JSON.parse);
  }
}

const target = await findPageTarget();
const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((r) => ws.addEventListener('open', r, { once: true }));
const cdp = new CDP(ws);
await cdp.send('Runtime.enable');

for (let i = 0; i < 40; i += 1) {
  if (await cdp.eval(`document.querySelectorAll('#viewer canvas').length > 0`)) break;
  await sleep(500);
}

/* ---------------- the shell fits the viewport ---------------- */

const shell = await cdp.json(`(() => {
  const app = document.querySelector('.app');
  const vc = document.getElementById('viewer-container');
  return {
    window: window.innerHeight,
    appH: Math.round(app.getBoundingClientRect().height),
    rootScrollH: document.documentElement.scrollHeight,
    rootClientH: document.documentElement.clientHeight,
    viewerH: Math.round(vc.getBoundingClientRect().height),
    viewerScrolls: vc.scrollHeight > vc.clientHeight,
    sidewaysScroll: document.body.scrollWidth > document.body.clientWidth,
  };
})()`);
console.log('shell:', shell);
check('app fills but does not exceed the viewport', shell.appH === shell.window, `${shell.appH} vs ${shell.window}`);
check(
  'the page itself never scrolls (only the panes do)',
  shell.rootScrollH <= shell.rootClientH,
  `${shell.rootScrollH} vs ${shell.rootClientH}`
);
check('no horizontal scroll', !shell.sidewaysScroll);
check('the PDF pane scrolls internally', shell.viewerScrolls);

/* ---------------- chat cards scroll rather than squash ---------------- */

await cdp.eval(`(() => {
  const messages = document.getElementById('messages');
  document.getElementById('chat-placeholder')?.remove();
  for (let i = 0; i < 25; i += 1) {
    const turn = document.createElement('div');
    turn.className = 'turn';
    const head = document.createElement('div');
    head.className = 'turn-user';
    head.textContent = 'Turn ' + i + ': tighten this paragraph and drop the hedging';
    const body = document.createElement('div');
    body.className = 'turn-body';
    body.textContent = 'Edited sections/model.tex. Build succeeded.';
    turn.append(head, body);
    messages.append(turn);
  }
  return true;
})()`);
await sleep(300);

const chat = await cdp.json(`(() => {
  const m = document.getElementById('messages');
  m.scrollTop = 1e6;
  const heights = [...document.querySelectorAll('.turn')].map((n) => n.getBoundingClientRect().height);
  return {
    clientH: m.clientHeight,
    scrollH: m.scrollHeight,
    canScroll: m.scrollHeight > m.clientHeight,
    scrolledTo: Math.round(m.scrollTop),
    minCard: Math.round(Math.min(...heights)),
    composerVisible:
      document.querySelector('.composer').getBoundingClientRect().bottom <= window.innerHeight + 1,
  };
})()`);
console.log('chat with 25 cards:', chat);
check('chat scrolls when cards overflow', chat.canScroll, `scrollH ${chat.scrollH} vs clientH ${chat.clientH}`);
check('chat can reach the bottom', chat.scrolledTo > 0, String(chat.scrolledTo));
check('cards keep their natural height', chat.minCard > 30, `${chat.minCard}px`);
check('the composer stays on screen', chat.composerVisible);

/* ---------------- chips scroll inside the composer ---------------- */

await cdp.eval(`(() => {
  const chips = document.getElementById('chips');
  for (let i = 0; i < 15; i += 1) {
    const chip = document.createElement('div');
    chip.className = 'chip';
    const head = document.createElement('div');
    head.className = 'chip-head';
    const ref = document.createElement('span');
    ref.className = 'chip-ref';
    ref.textContent = 'sections/model.tex:' + i;
    head.append(ref);
    const input = document.createElement('input');
    input.type = 'text';
    chip.append(head, input);
    chips.append(chip);
  }
  return true;
})()`);
await sleep(300);

const chips = await cdp.json(`(() => {
  const c = document.getElementById('chips');
  return {
    clientH: c.clientHeight,
    scrollH: c.scrollHeight,
    canScroll: c.scrollHeight > c.clientHeight,
    firstChipH: Math.round(c.firstElementChild.getBoundingClientRect().height),
    composerVisible:
      document.querySelector('.composer').getBoundingClientRect().bottom <= window.innerHeight + 1,
  };
})()`);
console.log('composer with 15 chips:', chips);
check('chips scroll inside the composer', chips.canScroll);
check('chips keep their height', chips.firstChipH > 20, `${chips.firstChipH}px`);
check('many chips do not push the composer off screen', chips.composerVisible);

/* ---------------- the inactive tab is really gone ---------------- */

await cdp.eval(`document.getElementById('tab-transcript').click(); true`);
await sleep(200);
const tabs = await cdp.json(`(() => {
  const m = document.getElementById('messages');
  const t = document.getElementById('transcript');
  for (let i = 0; i < 200; i += 1) t.append(document.createTextNode('line ' + i + '\\n'));
  t.scrollTop = 1e6;
  return {
    chatBox: Math.round(m.getBoundingClientRect().height),
    transcriptVisible: getComputedStyle(t).display !== 'none',
    transcriptScrolls: t.scrollHeight > t.clientHeight,
    transcriptScrolledTo: Math.round(t.scrollTop),
  };
})()`);
console.log('transcript tab:', tabs);
check('the chat pane takes no space when hidden', tabs.chatBox === 0, `${tabs.chatBox}px`);
check('the transcript is visible', tabs.transcriptVisible);
check('the transcript scrolls', tabs.transcriptScrolls && tabs.transcriptScrolledTo > 0);

/* ---------------- change markers: the coordinate round trip ---------------- */

const toggle = await cdp.json(`(() => {
  const b = document.getElementById('toggle-marks');
  return { exists: !!b, active: b?.classList.contains('active') ?? null };
})()`);
check('the Show changes toggle exists', toggle.exists);
check('markers are off until asked for', toggle.active === false);

// A marker is placed by running the click transform backwards. Feed a known
// point through both directions and it must come back where it started —
// this is the arithmetic that decides whether a marker lands on the right line.
const roundTrip = await cdp.json(`(() => {
  const { viewer } = window.__texai;
  const entry = viewer.pageEntry(1);
  const span = entry.el.querySelector('.textLayer span');
  const r = span.getBoundingClientRect();
  const clientX = r.left + r.width / 2;
  const clientY = r.top + r.height / 2;

  const pdf = viewer.clientPointToPdf(entry, clientX, clientY);
  const back = viewer.pdfRectToPageRect(entry, { x: pdf.x, y: pdf.y, width: 0, height: 0 });
  const pageRect = entry.el.getBoundingClientRect();
  return {
    dx: Math.abs(pageRect.left + back.left - clientX),
    dy: Math.abs(pageRect.top + back.top - clientY),
    scale: viewer.scale,
  };
})()`);
console.log('coordinate round trip:', roundTrip);
check(
  'pdf point -> page rect is the exact inverse of page point -> pdf point',
  roundTrip.dx < 1.5 && roundTrip.dy < 1.5,
  `off by ${roundTrip.dx.toFixed(2)}, ${roundTrip.dy.toFixed(2)} px`
);

// And it must stay true after a zoom, since markers are redrawn on every render.
const afterZoom = await cdp.json(`(() => {
  const { viewer } = window.__texai;
  viewer.zoomIn();
  const entry = viewer.pageEntry(1);
  const box = { x: 72, y: 100, width: 468, height: 10 };
  const rect = viewer.pdfRectToPageRect(entry, box);
  return {
    scale: viewer.scale,
    width: Math.round(rect.width),
    expected: Math.round(box.width * viewer.scale),
    inside: rect.left >= -1 && rect.top >= -1,
  };
})()`);
console.log('marker geometry after zoom:', afterZoom);
check('marker width tracks the zoom level', Math.abs(afterZoom.width - afterZoom.expected) <= 2,
  `${afterZoom.width} vs ${afterZoom.expected}`);
check('marker stays inside the page box', afterZoom.inside);

/* ---------------- moving the view ---------------- */

// A line deep in the document, on a later page: scrolling to it has to both
// move the container and land on the right page.
const jumpTarget = await cdp.eval(
  `fetch('/api/locate?file=sections/results.tex&line=3').then(r => r.json()).then(JSON.stringify)`
).then(JSON.parse);
console.log('locate results.tex:3 ->', { found: jumpTarget.found, page: jumpTarget.page });
check('a source line locates to a page', jumpTarget.found === true && jumpTarget.page > 1, `page ${jumpTarget.page}`);

const jump = await cdp.json(`(() => {
  const { viewer } = window.__texai;
  const c = document.getElementById('viewer-container');
  c.scrollTop = 0;
  const before = c.scrollTop;
  const loc = ${JSON.stringify(jumpTarget)};
  const moved = viewer.scrollToBox(loc.page, loc.boxes[0]);
  viewer.flashBox(loc.page, loc.boxes[0]);
  const entry = viewer.pageEntry(loc.page);
  const flash = entry.el.querySelector('.flash');
  const box = flash ? flash.getBoundingClientRect() : null;
  const bounds = c.getBoundingClientRect();
  return {
    moved,
    before,
    after: Math.round(c.scrollTop),
    currentPage: viewer.currentPage,
    flashDrawn: !!flash,
    flashOnScreen: box ? box.top >= bounds.top - 2 && box.bottom <= bounds.bottom + 2 : false,
  };
})()`);
console.log('jump:', jump);
check('scrollToBox reports success', jump.moved === true);
check('the view actually scrolled', jump.after > jump.before, `${jump.before} -> ${jump.after}`);
check('the target page becomes current', jump.currentPage === jumpTarget.page, `page ${jump.currentPage}`);
check('a flash highlight is drawn', jump.flashDrawn);
check('the flash lands inside the viewport', jump.flashOnScreen);

// The flash is transient; it must clean itself up.
await sleep(2600);
check(
  'the flash removes itself',
  (await cdp.eval(`document.querySelectorAll('.flash').length`)) === 0
);

// SyncTeX answers leniently for a line past the end of a file — it matches the
// nearest record rather than reporting nothing — so assert the response is
// coherent rather than empty.
const outOfRange = await cdp.eval(
  `fetch('/api/locate?file=sections/model.tex&line=99999').then(r => r.json()).then(JSON.stringify)`
).then(JSON.parse);
console.log('out-of-range line ->', { found: outOfRange.found, page: outOfRange.page });
check(
  'an out-of-range line still answers coherently',
  outOfRange.found === false || (outOfRange.page >= 1 && outOfRange.boxes.length > 0),
  `found=${outOfRange.found} page=${outOfRange.page}`
);

// A path outside the project must be refused, not located.
const escaped = await cdp.eval(
  `fetch('/api/locate?file=' + encodeURIComponent('../../etc/passwd') + '&line=1').then(r => r.status)`
);
check('locate refuses paths outside the root', escaped === 404, String(escaped));

/* ---------------- the inline before/after card ---------------- */

// Drive the real drawing code with synthetic marks: no agent turn needed, and
// it exercises exactly the path a real change takes.
const card = await cdp.json(`(() => {
  const { marks, viewer } = window.__texai;
  const entry = viewer.pageEntry(1);
  // A multi-line change: two rendered lines, as a wrapped paragraph produces.
  const box = { page: 1, x: 72, y: 300, width: 468, height: 10 };
  const box2 = { page: 1, x: 72, y: 313, width: 468, height: 10 };
  const near = { page: 1, x: 72, y: 340, width: 468, height: 10 };

  marks.turnId = 't0001';
  marks.enabled = true;
  marks.collapsed.clear();
  marks.marks = [
    { id: 'aaa', file: 'sections/model.tex', newStart: 9, kind: 'replace',
      before: 'the parameter a reader is most likely to argue with, which makes this good.',
      after: 'the parameter most often contested, which makes this good.',
      beforeParts: [
        { text: 'the parameter ', changed: false },
        { text: 'a reader is most likely to argue with, ', changed: true },
        { text: 'which makes this good.', changed: false }],
      afterParts: [
        { text: 'the parameter ', changed: false },
        { text: 'most often contested, ', changed: true },
        { text: 'which makes this good.', changed: false }],
      accepted: false, boxes: [box, box2] },
    { id: 'bbb', file: 'sections/model.tex', newStart: 11, kind: 'insert',
      before: '', after: 'A newly added sentence.', accepted: false, boxes: [near] },
  ];
  marks.draw();

  const cards = [...entry.el.querySelectorAll('.mark-card')];
  const first = cards[0];
  const del = first.querySelector('.mark-del');
  const add = first.querySelector('.mark-add');
  const rects = cards.map(c => c.getBoundingClientRect());
  return {
    count: cards.length,
    delText: del?.textContent ?? '',
    addText: add?.textContent ?? '',
    delLine: del ? getComputedStyle(del.querySelector('.w-changed')).textDecorationLine : '',
    addLine: add ? getComputedStyle(add.querySelector('.w-changed')).textDecorationLine : '',
    delStruck: del ? [...del.querySelectorAll('.w-changed')].map(n => n.textContent).join('') : '',
    delPlain: del ? [...del.querySelectorAll('.w-same')].map(n => n.textContent).join('') : '',
    addMarked: add ? [...add.querySelectorAll('.w-changed')].map(n => n.textContent).join('') : '',
    delColor: del ? getComputedStyle(del).backgroundColor : '',
    addColor: add ? getComputedStyle(add).backgroundColor : '',
    buttons: [...first.querySelectorAll('button')].map(b => b.textContent),
    overlap: rects.length === 2 ? Math.round(rects[0].bottom - rects[1].top) : null,
    insertNote: cards[1]?.querySelector('.mark-none')?.textContent ?? '',
    bandCount: entry.el.querySelectorAll('.mark').length,
    cardTop: Math.round(first.getBoundingClientRect().top),
    lastBandBottom: Math.round(Math.max(...[...entry.el.querySelectorAll('.mark')]
      .slice(0, 2).map(n => n.getBoundingClientRect().bottom))),
    cardBelowLastBand: first.getBoundingClientRect().top >=
      Math.max(...[...entry.el.querySelectorAll('.mark')].slice(0, 2).map(n => n.getBoundingClientRect().bottom)) - 1,
  };
})()`);
console.log('card:', card);

check('a card is drawn per change', card.count === 2, String(card.count));
check('every rendered line of a change gets a band', card.bandCount === 3, `${card.bandCount} bands for 3 boxes`);
check('the card hangs below the last band, not the first', card.cardBelowLastBand === true,
  `card top ${card.cardTop}, last band bottom ${card.lastBandBottom}`);
check('unchanged context is kept', /which makes this good/.test(card.delText), card.delText.slice(0, 40));
check('only the changed words are struck', card.delLine.includes('line-through') && /argue with/.test(card.delStruck), card.delStruck);
check('surrounding words are not struck', !/which makes this good/.test(card.delStruck), card.delPlain.slice(0, 45));
check('only the new words are emphasised', /often contested/.test(card.addMarked) && !/which makes this good/.test(card.addMarked), card.addMarked);
check('added words are not struck through', !card.addLine.includes('line-through'), card.addLine);
check('the two are different colours', card.delColor !== card.addColor, `${card.delColor} vs ${card.addColor}`);
check('accept and reject are on the card', card.buttons.slice(0, 2).join(',') === 'Accept,Reject', card.buttons.join(','));
check('overlapping cards are pushed apart', card.overlap !== null && card.overlap <= 0, `${card.overlap}px overlap`);
check('a pure insertion says so', /new/.test(card.insertNote), card.insertNote);

// Clicking the band collapses the card; accepted changes start collapsed.
const collapse = await cdp.json(`(() => {
  const { marks, viewer } = window.__texai;
  const entry = viewer.pageEntry(1);
  entry.el.querySelector('.mark').click();
  const afterClick = entry.el.querySelectorAll('.mark-card').length;
  marks.marks[0].accepted = true;
  marks.collapsed.clear();
  marks.draw();
  return { afterClick, whenAccepted: entry.el.querySelectorAll('.mark-card').length };
})()`);
console.log('collapse:', collapse);
check('clicking a band collapses its card', collapse.afterClick === 1, String(collapse.afterClick));
check('an accepted change collapses to a band', collapse.whenAccepted === 1, String(collapse.whenAccepted));

await cdp.eval(`(() => { const m = window.__texai.marks; m.marks = []; m.enabled = false; m.draw(); return true; })()`);

/* ---------------- word-precise highlighting ---------------- */

// Back to the top first: the jump test above scrolled to page 4, and the viewer
// discards the text layer of pages far off screen, so page 1 would have no
// spans left to match against.
await cdp.eval(`(() => { document.getElementById('viewer-container').scrollTop = 0; return true; })()`);
for (let i = 0; i < 20; i += 1) {
  const spans = await cdp.eval(
    `window.__texai.viewer.pageEntry(1).el.querySelectorAll('.textLayer span').length`
  );
  if (spans > 20) break;
  await sleep(300);
}

// SyncTeX only reports the line box, so a one-word change would band the whole
// line. The changed words are matched against the rendered text layer instead.
const narrow = await cdp.json(`(() => {
  const { marks, viewer } = window.__texai;
  const entry = viewer.pageEntry(1);

  // Pick real rendered text off the page and pretend it is what changed.
  // The text layer splits into small spans, so take the widest short one.
  const pageRect = entry.el.getBoundingClientRect();
  const span = [...entry.el.querySelectorAll('.textLayer span')]
    .filter(s => s.textContent.trim().length >= 6 &&
                 s.getBoundingClientRect().width < pageRect.width * 0.4)
    .sort((a, b) => b.getBoundingClientRect().width - a.getBoundingClientRect().width)[0];
  if (!span) return { skip: true };
  const phrase = span.textContent.trim();
  const spanRect = span.getBoundingClientRect();
  // The band covers the whole line this word sits on.
  const lineTop = spanRect.top - pageRect.top;
  const [bx0, , , by1] = entry.viewport.viewBox;
  const topPdf = entry.viewport.convertToPdfPoint(0, lineTop);
  const box = { page: 1, x: 0, y: by1 - topPdf[1], width: 468, height: 12 };

  marks.turnId = 't0001';
  marks.enabled = true;
  marks.collapsed.clear();
  marks.marks = [{
    id: 'narrow1', file: 'a.tex', newStart: 1, kind: 'replace',
    before: 'x', after: phrase, accepted: false,
    afterParts: [{ text: phrase, changed: true }],
    boxes: [box],
  }];
  marks.draw();

  const bands = [...entry.el.querySelectorAll('.mark')];
  const widths = bands.map(b => Math.round(b.getBoundingClientRect().width));
  return {
    phrase,
    bands: bands.length,
    narrowCount: bands.filter(b => b.classList.contains('narrow')).length,
    widths,
    lineWidth: Math.round(entry.viewport.width * (468 / (entry.viewport.viewBox[2] - entry.viewport.viewBox[0]))),
    spanWidth: Math.round(spanRect.width),
  };
})()`);
console.log('narrow:', narrow);
check('a test phrase was found on the page', !narrow.skip);
check('the changed words are found in the text layer', narrow.narrowCount > 0, `${narrow.narrowCount} of ${narrow.bands}`);
check(
  'the highlight is narrower than the whole line',
  narrow.widths.length > 0 && Math.max(...narrow.widths) < narrow.lineWidth * 0.9,
  `${Math.max(...narrow.widths)}px vs a ${narrow.lineWidth}px line`
);

// A rewrapped paragraph spans several lines while only a few words differ.
// The lines that match nothing must stay clean, not fall back to a line band.
const spread = await cdp.json(`(() => {
  const { marks, viewer } = window.__texai;
  const entry = viewer.pageEntry(1);
  const pageRect = entry.el.getBoundingClientRect();
  const span = [...entry.el.querySelectorAll('.textLayer span')]
    .filter(s => s.textContent.trim().length >= 6 &&
                 s.getBoundingClientRect().width < pageRect.width * 0.4)
    .sort((a, b) => b.getBoundingClientRect().width - a.getBoundingClientRect().width)[0];
  if (!span) return { skip: true };

  const phrase = span.textContent.trim();
  const lineTop = span.getBoundingClientRect().top - pageRect.top;
  const [, , , by1] = entry.viewport.viewBox;
  const onLine = by1 - entry.viewport.convertToPdfPoint(0, lineTop)[1];
  const lineHeight = 12;

  // Three lines: the words live on the middle one only.
  marks.turnId = 't0001';
  marks.enabled = true;
  marks.collapsed.clear();
  marks.marks = [{
    id: 'spread1', file: 'a.tex', newStart: 1, kind: 'replace',
    before: 'x', after: phrase, accepted: false,
    afterParts: [{ text: phrase, changed: true }],
    boxes: [
      { page: 1, x: 0, y: onLine - 26, width: 468, height: lineHeight },
      { page: 1, x: 0, y: onLine, width: 468, height: lineHeight },
      { page: 1, x: 0, y: onLine + 26, width: 468, height: lineHeight },
    ],
  }];
  marks.draw();

  const bands = [...entry.el.querySelectorAll('.mark')];
  const widths = bands.map(b => Math.round(b.getBoundingClientRect().width));
  return {
    bands: bands.length,
    widths,
    wide: widths.filter(w => w > pageRect.width * 0.6).length,
    narrowCount: bands.filter(b => b.classList.contains('narrow')).length,
  };
})()`);
console.log('spread:', spread);
check('lines with no changed words are left clean', spread.wide === 0,
  `${spread.wide} full-width band(s) among ${JSON.stringify(spread.widths)}`);
check('only the matched words are banded', spread.narrowCount === spread.bands && spread.bands > 0,
  `${spread.narrowCount} narrow of ${spread.bands}`);

// Words that cannot be matched must keep the full-line band, not vanish.
const fallback = await cdp.json(`(() => {
  const { marks, viewer } = window.__texai;
  const entry = viewer.pageEntry(1);
  marks.marks = [{
    id: 'nope', file: 'a.tex', newStart: 1, kind: 'replace',
    before: 'x', after: 'zzqq unmatchable gibberish', accepted: false,
    afterParts: [{ text: 'zzqq unmatchable gibberish', changed: true }],
    boxes: [{ page: 1, x: 72, y: 300, width: 468, height: 10 }],
  }];
  marks.draw();
  const bands = [...entry.el.querySelectorAll('.mark')];
  return { bands: bands.length, narrow: bands.filter(b => b.classList.contains('narrow')).length };
})()`);
console.log('fallback:', fallback);
check('unmatchable text falls back to the line band', fallback.bands === 1 && fallback.narrow === 0,
  JSON.stringify(fallback));

// Math and macros never appear literally in the render, so they are not searched for.
const mathOnly = await cdp.eval(`(() => {
  const { marks, viewer } = window.__texai;
  marks.marks = [{
    id: 'math', file: 'a.tex', newStart: 1, kind: 'replace',
    before: 'x', after: '$\\\\alpha$', accepted: false,
    afterParts: [{ text: '$\\\\alpha$', changed: true }],
    boxes: [{ page: 1, x: 72, y: 300, width: 468, height: 10 }],
  }];
  marks.draw();
  return viewer.pageEntry(1).el.querySelectorAll('.mark').length;
})()`);
check('a purely mathematical change still gets its line band', mathOnly === 1, String(mathOnly));

await cdp.eval(`(() => { const m = window.__texai.marks; m.marks = []; m.enabled = false; m.draw(); return true; })()`);

const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} layout checks passed`);
process.exit(failed.length ? 1 : 0);
