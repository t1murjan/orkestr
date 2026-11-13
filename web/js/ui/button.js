import { h } from '../core/dom.js';
import { icon } from './icons.js';

/**
 * button({ label, icon, iconRight, variant: 'primary'|'secondary'|'quiet'|'danger', size: 'sm'|'md'|'lg',
 *          onClick, disabled, loading, title, type, kbd })
 * node.update({ label, icon, disabled, loading })
 */
export function button(props = {}) {
  const el = h('button', { type: props.type || 'button', title: props.title });
  const state = { variant: 'secondary', size: 'md', ...props };
  const render = () => {
    el.className = `btn btn--${state.variant} btn--${state.size}` + (state.loading ? ' is-loading' : '');
    el.disabled = !!(state.disabled || state.loading);
    el.setAttribute('aria-busy', state.loading ? 'true' : 'false');
    const kids = [];
    if (state.icon) kids.push(icon(state.icon, { className: 'btn__icon' }));
    if (state.label != null) kids.push(h('span.btn__label', state.label));
    if (state.iconRight) kids.push(icon(state.iconRight, { className: 'btn__icon' }));
    if (state.kbd) kids.push(h('kbd.keycap', state.kbd));
    el.replaceChildren(...kids);
  };
  if (state.onClick) el.addEventListener('click', (e) => { if (!el.disabled) state.onClick(e); });
  render();
  el.update = (patch) => { Object.assign(state, patch); render(); };
  return el;
}

/** iconButton({ icon, label (required, used for aria-label and title), onClick, variant='quiet', size='md', pressed }) */
export function iconButton(props = {}) {
  const el = h('button', { type: 'button', title: props.label, 'aria-label': props.label });
  const state = { variant: 'quiet', size: 'md', ...props };
  const render = () => {
    el.className = `btn btn--${state.variant} btn--${state.size} btn--icon`;
    el.disabled = !!state.disabled;
    if (state.pressed != null) el.setAttribute('aria-pressed', state.pressed ? 'true' : 'false');
    el.replaceChildren(icon(state.icon));
  };
  if (state.onClick) el.addEventListener('click', (e) => { if (!el.disabled) state.onClick(e); });
  render();
  el.update = (patch) => { Object.assign(state, patch); render(); };
  return el;
}
