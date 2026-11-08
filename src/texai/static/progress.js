// A progress bar for a running turn, filled against how long such turns take.
//
// The server sends an estimate with every turn, phase by phase — the model
// writing, then the compile — taken from the recent turns that went the same
// way (see texai.timing.estimate_turn). The bar moves through each phase at
// the expected pace and jumps to the next when the server says it began, so a
// quick edit and a long agent turn each fill at their own speed. A phase that
// runs over slows towards the end of its share rather than reaching it: the
// bar never claims to be done before the turn is.

const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
};

// Share of a phase filled when its expected time is up; the rest is the
// slow approach for a phase that runs over.
const ON_TIME = 0.85;
const TICK_MS = 100;

const LABELS = {
  writing: 'Writing',
  compiling: 'Compiling',
  fixing: 'Fixing the build',
};

function seconds(ms) {
  const s = Math.max(1, Math.round(ms / 1000));
  return `${s}s`;
}

export class ProgressBar {
  /** `labels` overrides the phase names shown, e.g. {writing: 'Agent working'}. */
  constructor(labels = {}) {
    this.labels = { ...LABELS, ...labels };
    this.el = el('div', 'progress');
    this.track = el('div', 'progress-track');
    this.fill = el('div', 'progress-fill');
    this.track.append(this.fill);
    this.label = el('div', 'progress-label');
    this.el.append(this.track, this.label);
    this.phases = [];
    this.index = 0;
    this.timer = null;
  }

  /** Start filling against `estimate`; `startedAt` (ms) for a turn already underway. */
  start(estimate, startedAt = Date.now()) {
    this.estimate = estimate;
    this.phases = (estimate?.phases || []).map((p) => ({ ...p }));
    if (!this.phases.length) this.phases = [{ name: 'writing', ms: 8000 }];
    this.index = 0;
    this.phaseStart = startedAt;
    clearInterval(this.timer);
    this.timer = setInterval(() => this._render(), TICK_MS);
    this._render();
  }

  /** The server says phase `name` began: move to it. */
  phase(name) {
    const next = this.phases.findIndex((p, i) => i >= this.index && p.name === name);
    if (next < 0) return;
    this.index = next;
    this.phaseStart = Date.now();
    this._render();
  }

  /** The build failed and the agent is fixing it: a fix, then another compile. */
  retry() {
    const compile = this.phases.find((p) => p.name === 'compiling')?.ms ?? 2000;
    this.phases.splice(this.index + 1, 0,
      { name: 'fixing', ms: this.estimate?.fixMs ?? 8000 },
      { name: 'compiling', ms: compile });
    this.phase('fixing');
  }

  finish() {
    clearInterval(this.timer);
    this.timer = null;
    this.fill.style.width = '100%';
    this.label.textContent = '';
  }

  stop() {
    clearInterval(this.timer);
    this.timer = null;
  }

  _render() {
    const total = this.phases.reduce((sum, p) => sum + p.ms, 0) || 1;
    const before = this.phases.slice(0, this.index).reduce((sum, p) => sum + p.ms, 0);
    const current = this.phases[this.index];
    const elapsed = Date.now() - this.phaseStart;
    const x = current.ms ? elapsed / current.ms : 1;
    const share = x <= 1 ? ON_TIME * x : ON_TIME + (1 - ON_TIME) * (1 - Math.exp(-(x - 1) * 1.5));
    const done = before + current.ms * share;
    this.fill.style.width = `${Math.min(99, (100 * done) / total).toFixed(1)}%`;

    const left = total - before - Math.min(elapsed, current.ms);
    const name = this.labels[current.name] || current.name;
    this.label.textContent = elapsed > current.ms * 1.5
      ? `${name}… taking longer than usual`
      : `${name}… about ${seconds(left)} left`;
  }
}

/** What a turn's first phase is called on its bar: whoever is doing the writing. */
export function progressLabels(turn) {
  const route = turn.timing?.route;
  if (route === 'quick') return { writing: 'Writing the edit' };
  return { writing: 'Agent working' };
}
