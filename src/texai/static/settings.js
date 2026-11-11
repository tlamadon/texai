// Which model the agent uses and how hard it thinks: one quiet button on the
// composer, beside the message it will apply to.
//
// The server holds the choice (it starts from --model and --effort) and says
// what there is to choose from; `null` for either means the agent's own
// default. A change closes the session behind the conversation, and the next
// turn reopens the same conversation with the new settings — so it is refused
// while a turn is running, and the button is disabled then.

import { postJSON } from './api.js';
import { showToast } from './toast.js';

const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};

export class AgentSettingsMenu {
  constructor() {
    this.els = {
      wrap: document.getElementById('agent-settings'),
      button: document.getElementById('agent-settings-button'),
      label: document.getElementById('agent-settings-label'),
      pop: document.getElementById('agent-settings-menu'),
    };
    this.state = { model: null, effort: null, models: [], efforts: [] };
    this.busy = false;
    this._wire();
  }

  _wire() {
    const { button, pop } = this.els;
    button.addEventListener('click', () => this.toggle(pop.hidden));
    // Click-away and Escape, as the other menus close.
    document.addEventListener('click', (event) => {
      if (pop.hidden || button.contains(event.target) || pop.contains(event.target)) return;
      this.toggle(false);
    });
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && !pop.hidden) this.toggle(false);
    });
  }

  /** What /api/info, or an `agent_settings` event, says is in use. */
  set(agent) {
    if (!agent) return;
    for (const key of ['model', 'effort', 'models', 'efforts']) {
      if (key in agent) this.state[key] = agent[key];
    }
    // Nothing to choose with an agent that cannot be reached.
    this.els.wrap.hidden = agent.available === false;
    this._render();
  }

  setBusy(busy) {
    this.busy = busy;
    this.els.button.disabled = busy;
    if (busy) this.toggle(false);
  }

  toggle(open) {
    const { button, pop } = this.els;
    pop.hidden = !open;
    button.setAttribute('aria-expanded', String(open));
    button.classList.toggle('on', open);
    if (open) this._render();
  }

  async choose(change) {
    const next = { model: this.state.model, effort: this.state.effort, ...change };
    this.toggle(false);
    if (next.model === this.state.model && next.effort === this.state.effort) return;
    try {
      this.set(await postJSON('/api/agent/settings', next));
    } catch (err) {
      showToast(err.message || String(err), { type: 'error' });
    }
  }

  _render() {
    const { model, effort, models, efforts } = this.state;
    const { button, label, pop } = this.els;
    label.textContent = model || effort
      ? [model || 'default', effort].filter(Boolean).join(' · ')
      : 'Default model';
    button.title = `Model: ${model || 'the agent’s default'} — effort: ${effort || 'default'}`;
    if (pop.hidden) return;

    const item = (text, on, pick) => {
      const node = el('button', 'menu-item check', text);
      node.type = 'button';
      node.setAttribute('role', 'menuitemradio');
      node.setAttribute('aria-pressed', String(on));
      node.addEventListener('click', pick);
      return node;
    };
    // A model named on the command line, or typed in, is a choice too.
    const listed = model && !models.includes(model) ? [...models, model] : models;
    const other = item('Other…', false, () => {
      const typed = window.prompt('Model id or alias', model || '');
      if (typed?.trim()) this.choose({ model: typed.trim() });
    });
    pop.replaceChildren(
      el('div', 'menu-label', 'Model'),
      item('Default', !model, () => this.choose({ model: null })),
      ...listed.map((name) => item(name, name === model, () => this.choose({ model: name }))),
      other,
      el('div', 'menu-rule'),
      el('div', 'menu-label', 'Effort'),
      item('Default', !effort, () => this.choose({ effort: null })),
      ...efforts.map((name) => item(name, name === effort, () => this.choose({ effort: name }))),
    );
  }
}
