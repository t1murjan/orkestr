// Content components: tabs, table, card, statTile, emptyState, skeleton, codeBlock.
import { h } from '../core/dom.js';
import { icon } from './icons.js';
import { button, iconButton } from './button.js';
import { fmtSigned, pctChange, fmtInt } from '../core/format.js';
import { tweenNumber } from '../core/motion.js';

/**
 * tabs({ tabs: [{ id, label, count }], value, onChange(id), ariaLabel }) -> tablist. node.value get/set.
 * The screen swaps its own content; give the panel role="tabpanel" if you like.
 */
export function tabs(props = {}) {
  let value = props.value ?? (props.tabs[0] && props.tabs[0].id);
  const btns = props.tabs.map((t) => h('button', { type: 'button', class: 'tab', role: 'tab', dataset: { id: t.id } },
    h('span', t.label), t.count != null ? h('span.tab__count', String(t.count)) : null));
  const el = h('div', { class: 'tabs', role: 'tablist', 'aria-label': props.ariaLabel }, btns);
  const sync = () => btns.forEach((b, i) => {
    const on = props.tabs[i].id === value;
    b.setAttribute('aria-selected', on ? 'true' : 'false');
    b.tabIndex = on ? 0 : -1;
    b.classList.toggle('is-on', on);
  });
  const set = (v, fire) => { if (v === value) return; value = v; sync(); if (fire && props.onChange) props.onChange(v); };
  btns.forEach((b, i) => {
    b.addEventListener('click', () => set(props.tabs[i].id, true));
    b.addEventListener('keydown', (e) => {
      const d = e.key === 'ArrowRight' ? 1 : e.key === 'ArrowLeft' ? -1 : 0;
      if (!d) return;
      e.preventDefault();
      const n = (i + d + btns.length) % btns.length;
      btns[n].focus(); set(props.tabs[n].id, true);
    });
  });
  Object.defineProperty(el, 'value', { get: () => value, set: (v) => set(v, false) });
  el.setCount = (id, n) => { const i = props.tabs.findIndex((t) => t.id === id); const c = btns[i] && btns[i].querySelector('.tab__count'); if (c) c.textContent = String(n); };
  sync();
  return el;
}

/**
 * table({ columns, rows, rowKey = r => r.id, selectable, selected (key), onSelect(row), onRowClick(row), sort: {key, dir}, onSort(key, dir),
 *         empty: Node, dense, ariaLabel, rowRef(tr, row) })
 * column: { key, label, align: 'left'|'right', width, mono, sortable, render(row) -> Node|string, title }
 * Numeric columns: align 'right' (tabular figures). node.update({ rows, selected, sort }).
 * Row states: hover, selected (raised + accent marker), keyboard focus (inset ring). Arrows move between rows.
 */
export function table(props = {}) {
  const state = { rows: [], selected: null, sort: null, ...props };
  const clickable = !!(props.selectable || props.onRowClick);
  const rowKey = props.rowKey || ((r) => r.id);
  const thead = h('thead');
  const tbody = h('tbody');
  const emptyHost = h('div.table__empty');
  const el = h('div', { class: ['table-wrap', props.dense && 'table-wrap--dense'] },
    h('table', { class: 'table', 'aria-label': props.ariaLabel }, thead, tbody), emptyHost);
  const rowEls = new Map();

  const renderHead = () => {
    thead.replaceChildren(h('tr', props.columns.map((c) => {
      const sorted = state.sort && state.sort.key === c.key;
      const th = h('th', { class: [c.align === 'right' && 'is-right', c.sortable && 'is-sortable'], scope: 'col', style: c.width ? { width: typeof c.width === 'number' ? c.width + 'px' : c.width } : null,
        'aria-sort': sorted ? (state.sort.dir === 'asc' ? 'ascending' : 'descending') : null }, c.sortable
        ? h('button', { type: 'button', class: 'th-btn', onclick: () => {
          const dir = sorted && state.sort.dir === 'desc' ? 'asc' : 'desc';
          state.sort = { key: c.key, dir };
          if (props.onSort) props.onSort(c.key, dir);
          renderHead();
        } }, h('span', c.label), sorted ? icon(state.sort.dir === 'asc' ? 'chevron-up' : 'chevron-down', { size: 12 }) : null)
        : h('span', c.label));
      return th;
    })));
  };

  const cell = (c, row) => {
    const content = c.render ? c.render(row) : row[c.key];
    return h('td', { class: [c.align === 'right' && 'is-right', c.mono && 'is-mono'] }, content == null ? '' : content);
  };

  const renderRows = () => {
    const keep = new Set();
    const frag = [];
    for (const row of state.rows) {
      const k = rowKey(row);
      keep.add(k);
      let tr = rowEls.get(k);
      if (!tr) {
        tr = h('tr', { class: 'tr', tabindex: clickable ? 0 : null });
        if (clickable) {
          tr.addEventListener('click', (e) => { if (e.target.closest('button, a, input, select')) return; activate(tr.__row); });
          tr.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' || e.key === ' ') { if (e.target === tr) { e.preventDefault(); activate(tr.__row); } }
            else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
              const sib = e.key === 'ArrowDown' ? tr.nextElementSibling : tr.previousElementSibling;
              if (sib) { e.preventDefault(); sib.focus(); }
            }
          });
        }
        rowEls.set(k, tr);
      }
      tr.__row = row;
      tr.replaceChildren(...props.columns.map((c) => cell(c, row)));
      tr.classList.toggle('is-selected', state.selected != null && state.selected === k);
      if (props.selectable) tr.setAttribute('aria-selected', state.selected === k ? 'true' : 'false');
      if (props.rowRef) props.rowRef(tr, row);
      frag.push(tr);
    }
    for (const [k, tr] of rowEls) if (!keep.has(k)) { tr.remove(); rowEls.delete(k); }
    // reorder only when needed so a focused row keeps focus
    frag.forEach((tr, i) => { if (tbody.children[i] !== tr) tbody.insertBefore(tr, tbody.children[i] || null); });
    emptyHost.replaceChildren(...(state.rows.length ? [] : [props.empty || h('div.table__empty-text', 'Нет данных')]));
    emptyHost.hidden = state.rows.length > 0;
  };

  const activate = (row) => {
    if (props.selectable) {
      state.selected = rowKey(row);
      for (const [k, tr] of rowEls) {
        tr.classList.toggle('is-selected', k === state.selected);
        tr.setAttribute('aria-selected', k === state.selected ? 'true' : 'false');
      }
      if (props.onSelect) props.onSelect(row);
    }
    if (props.onRowClick) props.onRowClick(row);
  };

  el.update = (p = {}) => {
    Object.assign(state, p);
    if ('sort' in p) renderHead();
    renderRows();
  };
  renderHead();
  renderRows();
  return el;
}

/** card({ title, subtitle, actions: Node|Node[], children, padding: 'md'|'lg'|'none', className }) -> <section class="card">; node.body is the content box */
export function card({ title, subtitle, actions, children, padding = 'md', className = '' } = {}) {
  const body = h('div.card__body', children);
  const head = (title || actions) ? h('header.card__head',
    h('div.card__titles', title ? h('h3.card__title', title) : null, subtitle ? h('p.card__sub', subtitle) : null),
    actions ? h('div.card__actions', actions) : null) : null;
  const el = h('section', { class: ['card', `card--pad-${padding}`, className] }, head, body);
  el.body = body; el.head = head;
  return el;
}

/**
 * Stat tile: a label, a figure and a change that says what it compares to. Numbers only.
 * statTile({ label, value, format = fmtInt, unit, change: { delta (percent) | text, vs: 'к предыдущему часу', goodWhen: 'up'|'down' } })
 * node.update({ value, change }). A numeric value tweens.
 */
export function statTile(props = {}) {
  const fmt = props.format || ((n) => (typeof n === 'number' ? fmtInt(n) : String(n ?? '—')));
  const figure = h('span.stat__figure');
  const unit = h('span.stat__unit');
  const change = h('div.stat__change');
  const el = h('div.stat', h('div.stat__label', props.label), h('div.stat__value', figure, unit), change);
  const state = { value: null, change: null };
  const renderChange = (c) => {
    if (!c) { change.replaceChildren(); change.hidden = true; return; }
    change.hidden = false;
    const delta = c.delta != null ? c.delta : (c.cur != null ? pctChange(c.cur, c.prev) : null);
    const txt = c.text ?? (delta == null ? '—' : fmtSigned(delta, { digits: Math.abs(delta) < 10 && Math.abs(delta) % 1 !== 0 ? 1 : 0 }));
    let tone = 'flat';
    if (delta != null && Math.abs(delta) >= 0.05) {
      const up = delta > 0;
      tone = c.goodWhen ? ((c.goodWhen === 'up') === up ? 'good' : 'bad') : 'flat';
    }
    change.dataset.tone = tone;
    change.replaceChildren(...[h('span.stat__delta', txt), c.vs ? h('span.stat__vs', c.vs) : null].filter(Boolean));
  };
  el.update = (p = {}) => {
    if ('value' in p) {
      state.value = p.value;
      if (typeof p.value === 'number') tweenNumber(figure, p.value, { format: fmt });
      else { figure.__num = null; figure.textContent = fmt(p.value); }
    }
    if ('unit' in p) unit.textContent = p.unit || '';
    if ('change' in p) renderChange(p.change);
  };
  el.update({ value: props.value, unit: props.unit, change: props.change });
  return el;
}

/** emptyState({ title, text, icon, action: { label, onClick, variant, icon }, secondary: same, compact }) -> one line saying what to do next */
export function emptyState({ title, text, icon: ic, action, secondary, compact = false, children } = {}) {
  return h('div', { class: ['empty', compact && 'empty--compact'] },
    ic ? h('div.empty__icon', icon(ic, { size: 20 })) : null,
    h('div.empty__title', title),
    text ? h('p.empty__text', text) : null,
    children || null,
    (action || secondary) ? h('div.empty__actions',
      action ? button({ variant: 'primary', ...action }) : null,
      secondary ? button({ variant: 'secondary', ...secondary }) : null) : null);
}

/** skeleton({ lines = 3, height, width }) -> static placeholder blocks (no shimmer: nothing loops) */
export function skeleton({ lines = 3, height = 12, width } = {}) {
  const rows = [];
  for (let i = 0; i < lines; i++) {
    const w = width || (i === lines - 1 && lines > 1 ? '60%' : '100%');
    rows.push(h('span.skeleton__line', { style: { height: height + 'px', width: typeof w === 'number' ? w + 'px' : w } }));
  }
  return h('div', { class: 'skeleton', 'aria-hidden': 'true' }, rows);
}

async function copyText(s) {
  try { await navigator.clipboard.writeText(s); return true; } catch (e) { /* fall through */ }
  const ta = h('textarea', { style: { position: 'fixed', opacity: '0', top: '0' } });
  ta.value = s;
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
  ta.remove();
  return ok;
}
export { copyText };

/** codeBlock({ code, label, copy = true, maxHeight }) -> <figure class="code"> with a copy button */
export function codeBlock({ code = '', label, copy = true, maxHeight } = {}) {
  const pre = h('pre.code__pre', h('code', code));
  if (maxHeight) pre.style.maxHeight = maxHeight + 'px';
  const btn = copy ? iconButton({ icon: 'copy', label: 'Скопировать', size: 'sm', onClick: async () => {
    const ok = await copyText(el.__code);
    btn.update({ icon: ok ? 'check' : 'x', label: ok ? 'Скопировано' : 'Не удалось скопировать' });
    setTimeout(() => btn.update({ icon: 'copy', label: 'Скопировать' }), 1600);
  } }) : null;
  const el = h('figure.code', label ? h('figcaption.code__label', label) : null, pre, btn ? h('div.code__copy', btn) : null);
  el.__code = code;
  el.update = (c) => { el.__code = c; pre.firstChild.textContent = c; };
  return el;
}
