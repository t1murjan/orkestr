// Form controls: input, textarea, field, select, segmented, switchControl.
import { h, uid, on } from '../core/dom.js';
import { icon } from './icons.js';
import { popover } from './overlay.js';

/**
 * input({ value, placeholder, type='text', size='md'|'sm', icon, suffix, mono, invalid, disabled, id, name, ariaLabel,
 *         onInput(value, event), onChange(value, event) })
 * -> <span class="input"> wrapper. node.input is the <input>, node.value get/set, node.focus(), node.update({invalid, disabled}).
 */
export function input(props = {}) {
  const field = h('input', {
    class: 'input__el', type: props.type || 'text', placeholder: props.placeholder, name: props.name, id: props.id,
    autocomplete: props.autocomplete || 'off', spellcheck: 'false', 'aria-label': props.ariaLabel, inputmode: props.inputmode,
  });
  field.value = props.value ?? '';
  const wrap = h('span', { class: ['input', `input--${props.size || 'md'}`, props.mono && 'input--mono'] },
    props.icon ? h('span.input__icon', icon(props.icon)) : null, field,
    props.suffix ? h('span.input__suffix', props.suffix) : null);
  if (props.disabled) field.disabled = true;
  if (props.invalid) { wrap.classList.add('is-invalid'); field.setAttribute('aria-invalid', 'true'); }
  if (props.onInput) field.addEventListener('input', (e) => props.onInput(field.value, e));
  if (props.onChange) field.addEventListener('change', (e) => props.onChange(field.value, e));
  wrap.input = field;
  Object.defineProperty(wrap, 'value', { get: () => field.value, set: (v) => { field.value = v ?? ''; } });
  wrap.focus = () => field.focus();
  wrap.update = (p) => {
    if ('invalid' in p) { wrap.classList.toggle('is-invalid', !!p.invalid); if (p.invalid) field.setAttribute('aria-invalid', 'true'); else field.removeAttribute('aria-invalid'); }
    if ('disabled' in p) field.disabled = !!p.disabled;
    if ('placeholder' in p) field.placeholder = p.placeholder || '';
    if ('value' in p) field.value = p.value ?? '';
  };
  return wrap;
}

/** textarea({ value, rows=4, placeholder, mono, invalid, id, ariaLabel, onInput(value, event) }) -> <textarea class="textarea"> */
export function textarea(props = {}) {
  const el = h('textarea', {
    class: ['textarea', props.mono && 'textarea--mono'], rows: props.rows || 4, placeholder: props.placeholder, id: props.id,
    'aria-label': props.ariaLabel, spellcheck: 'false',
  });
  el.value = props.value ?? '';
  if (props.invalid) { el.classList.add('is-invalid'); el.setAttribute('aria-invalid', 'true'); }
  if (props.onInput) el.addEventListener('input', (e) => props.onInput(el.value, e));
  el.update = (p) => {
    if ('invalid' in p) { el.classList.toggle('is-invalid', !!p.invalid); if (p.invalid) el.setAttribute('aria-invalid', 'true'); else el.removeAttribute('aria-invalid'); }
  };
  return el;
}

/**
 * field({ label, hint, error, control, required, id }) -> labelled block. node.update({ error, hint }).
 * Wires <label for> and aria-describedby to the control (input, textarea, select, ...).
 */
export function field({ label, hint, error, control, required = false, id } = {}) {
  const fid = id || uid('f');
  const target = control && (control.input || control);
  if (target && !target.id) target.id = fid;
  const labelEl = h('label.field__label', { for: target ? target.id : fid }, label, required ? h('span.field__req', { 'aria-hidden': 'true' }, ' *') : null);
  const hintEl = h('div.field__hint', { id: fid + '-hint' });
  const errEl = h('div.field__error', { id: fid + '-err', role: 'alert' });
  const el = h('div.field', labelEl, control, hintEl, errEl);
  const render = (nextHint, nextError) => {
    hintEl.textContent = nextHint || '';
    hintEl.hidden = !nextHint || !!nextError;
    errEl.textContent = nextError || '';
    errEl.hidden = !nextError;
    el.classList.toggle('has-error', !!nextError);
    if (control && typeof control.update === 'function') control.update({ invalid: !!nextError });
    if (target) {
      const ids = [!hintEl.hidden && hintEl.id, !errEl.hidden && errEl.id].filter(Boolean).join(' ');
      if (ids) target.setAttribute('aria-describedby', ids); else target.removeAttribute('aria-describedby');
    }
  };
  let cur = { hint, error };
  render(hint, error);
  el.update = (p) => { cur = { ...cur, ...p }; render(cur.hint, cur.error); };
  return el;
}

/**
 * Custom select: select({ options: [{ value, label, hint, disabled, mark: Node }], value, onChange(value), placeholder='Выберите',
 *                        disabled, size='md'|'sm', mono, ariaLabel, id })
 * -> trigger <button>. node.value get/set, node.setOptions(options), node.update({ disabled }).
 * Keyboard: Enter/Space/arrows open; arrows, Home, End, type-ahead move; Enter/Space choose; Esc closes.
 */
export function select(props = {}) {
  let options = props.options || [];
  let value = props.value ?? null;
  let pop = null;
  const labelEl = h('span.select__value');
  const trigger = h('button', {
    type: 'button', class: ['select', `select--${props.size || 'md'}`, props.mono && 'select--mono'], id: props.id,
    role: 'combobox', 'aria-haspopup': 'listbox', 'aria-expanded': 'false', 'aria-label': props.ariaLabel,
  }, labelEl, h('span.select__chev', icon('chevrons-up-down')));
  if (props.disabled) trigger.disabled = true;

  const current = () => options.find((o) => o.value === value) || null;
  const render = () => {
    const cur = current();
    labelEl.replaceChildren(...(cur ? [cur.mark, cur.label].filter(Boolean) : [props.placeholder || 'Выберите']));
    labelEl.classList.toggle('is-placeholder', !cur);
  };

  const close = (restore = true) => { if (pop) { const p = pop; pop = null; p.close(restore); } };
  const choose = (v) => {
    const changed = v !== value;
    value = v; render(); close();
    if (changed && props.onChange) props.onChange(v);
  };

  const open = () => {
    if (pop || trigger.disabled) return;
    let active = Math.max(0, options.findIndex((o) => o.value === value));
    if (options[active] && options[active].disabled) active = options.findIndex((o) => !o.disabled);
    const listId = uid('lb');
    const items = options.map((o, i) => h('li', {
      class: ['option', o.disabled && 'is-disabled'], role: 'option', id: `${listId}-${i}`, 'aria-selected': o.value === value ? 'true' : 'false',
      'aria-disabled': o.disabled ? 'true' : null,
      onpointermove: () => setActive(i),
      onclick: () => { if (!o.disabled) choose(o.value); },
    }, o.mark || null, h('span.option__label', o.label), o.hint ? h('span.option__hint', o.hint) : null,
    o.value === value ? h('span.option__check', icon('check')) : null));
    const list = h('ul', { class: 'listbox', role: 'listbox', tabindex: '-1', 'aria-activedescendant': `${listId}-${active}` }, items);
    const setActive = (i) => {
      active = i;
      items.forEach((it, k) => it.classList.toggle('is-active', k === i));
      list.setAttribute('aria-activedescendant', `${listId}-${i}`);
      items[i] && items[i].scrollIntoView({ block: 'nearest' });
    };
    const step = (dir) => {
      let i = active;
      for (let n = 0; n < options.length; n++) { i = (i + dir + options.length) % options.length; if (!options[i].disabled) break; }
      setActive(i);
    };
    let typed = '', typedT = 0;
    list.addEventListener('keydown', (e) => {
      if (e.key === 'ArrowDown') { e.preventDefault(); step(1); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); step(-1); }
      else if (e.key === 'Home') { e.preventDefault(); setActive(options.findIndex((o) => !o.disabled)); }
      else if (e.key === 'End') { e.preventDefault(); let i = options.length - 1; while (i > 0 && options[i].disabled) i--; setActive(i); }
      else if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); if (options[active] && !options[active].disabled) choose(options[active].value); }
      else if (e.key === 'Tab') { close(false); }
      else if (e.key.length === 1 && !e.ctrlKey && !e.metaKey) {
        clearTimeout(typedT); typed += e.key.toLowerCase(); typedT = setTimeout(() => { typed = ''; }, 600);
        const i = options.findIndex((o) => !o.disabled && String(o.label).toLowerCase().startsWith(typed));
        if (i >= 0) setActive(i);
      }
    });
    pop = popover({ anchor: trigger, content: list, placement: 'bottom-start', offset: 4, matchWidth: true, className: 'popover--list', role: 'presentation', onClose: () => { trigger.setAttribute('aria-expanded', 'false'); pop = null; } });
    trigger.setAttribute('aria-expanded', 'true');
    trigger.setAttribute('aria-controls', listId);
    setActive(active);
    list.focus();
  };

  trigger.addEventListener('click', () => (pop ? close() : open()));
  trigger.addEventListener('keydown', (e) => {
    if (['ArrowDown', 'ArrowUp'].includes(e.key)) { e.preventDefault(); open(); }
  });
  Object.defineProperty(trigger, 'value', { get: () => value, set: (v) => { value = v; render(); } });
  trigger.setOptions = (opts) => { options = opts; render(); };
  trigger.update = (p) => { if ('disabled' in p) trigger.disabled = !!p.disabled; if ('invalid' in p) trigger.classList.toggle('is-invalid', !!p.invalid); };
  render();
  return trigger;
}

/**
 * segmented({ options: [{ value, label, icon, title }], value, onChange(value), size='md'|'sm', ariaLabel })
 * -> radiogroup. node.value get/set. Arrow keys move and select.
 */
export function segmented(props = {}) {
  let value = props.value ?? (props.options[0] && props.options[0].value);
  const btns = props.options.map((o) => h('button', {
    type: 'button', class: 'seg__item', role: 'radio', title: o.title, dataset: { value: o.value },
  }, o.icon ? icon(o.icon) : null, o.label != null ? h('span', o.label) : null));
  const el = h('div', { class: ['seg', `seg--${props.size || 'md'}`], role: 'radiogroup', 'aria-label': props.ariaLabel }, btns);
  const sync = () => btns.forEach((b, i) => {
    const on = props.options[i].value === value;
    b.setAttribute('aria-checked', on ? 'true' : 'false');
    b.tabIndex = on ? 0 : -1;
    b.classList.toggle('is-on', on);
  });
  const set = (v, fire) => { if (v === value) return; value = v; sync(); if (fire && props.onChange) props.onChange(v); };
  btns.forEach((b, i) => {
    b.addEventListener('click', () => set(props.options[i].value, true));
    b.addEventListener('keydown', (e) => {
      const d = e.key === 'ArrowRight' || e.key === 'ArrowDown' ? 1 : e.key === 'ArrowLeft' || e.key === 'ArrowUp' ? -1 : 0;
      if (!d) return;
      e.preventDefault();
      const n = (i + d + btns.length) % btns.length;
      btns[n].focus(); set(props.options[n].value, true);
    });
  });
  Object.defineProperty(el, 'value', { get: () => value, set: (v) => set(v, false) });
  sync();
  return el;
}

/** switchControl({ checked, onChange(checked), label, hint, disabled, ariaLabel }) -> <label class="switch">; node.checked get/set */
export function switchControl(props = {}) {
  let checked = !!props.checked;
  const btn = h('button', { type: 'button', class: 'switch__track', role: 'switch', 'aria-checked': String(checked), 'aria-label': props.ariaLabel || (props.label ? null : 'Переключатель') }, h('span.switch__thumb'));
  if (props.disabled) btn.disabled = true;
  const labelId = uid('sw');
  const el = h('div', { class: 'switch' }, btn, props.label ? h('span', { class: 'switch__text', id: labelId, onclick: () => !btn.disabled && btn.click() }, h('span.switch__label', props.label), props.hint ? h('span.switch__hint', props.hint) : null) : null);
  if (props.label) btn.setAttribute('aria-labelledby', labelId);
  const set = (v, fire) => { checked = !!v; btn.setAttribute('aria-checked', String(checked)); if (fire && props.onChange) props.onChange(checked); };
  btn.addEventListener('click', () => set(!checked, true));
  Object.defineProperty(el, 'checked', { get: () => checked, set: (v) => set(v, false) });
  el.update = (p) => { if ('disabled' in p) btn.disabled = !!p.disabled; };
  return el;
}
