// Transient status messages in the bottom-right corner.

const STACK_ID = 'toast-stack';
const DEFAULT_MS = 2600;

export function showToast(message, { type = 'info', timeout = DEFAULT_MS } = {}) {
  const stack = document.getElementById(STACK_ID);
  if (!stack) return;

  const el = document.createElement('div');
  el.className = type === 'error' ? 'toast error' : 'toast';
  el.textContent = message;
  stack.append(el);

  const life = type === 'error' ? Math.max(timeout, 5000) : timeout;
  setTimeout(() => {
    el.classList.add('fading');
    setTimeout(() => el.remove(), 320);
  }, life);
}
