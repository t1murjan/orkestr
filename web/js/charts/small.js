import { h, svg } from '../core/dom.js';
import { tweenNumber, tweenValues } from '../core/motion.js';
import { fmtInt, fmtPercent } from '../core/format.js';
import { smoothPath } from './util.js';

/**
 * Ranked list with thin bars: rankedList({ items: [{ id, label, value, detail, mark: Node }], format = fmtInt, max, accent = 'first'|'none', onSelect(item), ariaLabel })
 * The leader's bar takes the accent, the rest are neutral. node.update({ items }) tweens values and bars (scaleX).
 */
export function rankedList(props = {}) {
  const o = { format: fmtInt, accent: 'first', ...props };
  const el = h('ul', { class: 'ranked', 'aria-label': o.ariaLabel });
  const rows = new Map();
  const render = (items) => {
    const max = o.max != null ? o.max : Math.max(1, ...items.map((i) => i.value || 0));
    const keep = new Set();
    items.forEach((it, idx) => {
      keep.add(it.id);
      let r = rows.get(it.id);
      if (!r) {
        const valueEl = h('span.ranked__value');
        const fill = h('span.ranked__fill');
        const labelEl = h('span.ranked__label.truncate');
        const detail = h('span.ranked__detail');
        const markHost = h('span.ranked__mark');
        const main = o.onSelect ? h('button', { type: 'button', class: 'ranked__main', onclick: () => o.onSelect(r.item) }) : h('div.ranked__main');
        main.append(markHost, labelEl, detail, valueEl);
        r = { li: h('li.ranked__row', main, h('span.ranked__track', fill)), valueEl, fill, labelEl, detail, markHost, main };
        rows.set(it.id, r);
      }
      r.item = it;
      r.labelEl.textContent = it.label;
      r.labelEl.title = it.label;
      r.detail.textContent = it.detail || '';
      r.detail.hidden = !it.detail;
      if (it.mark && r.markHost.firstChild !== it.mark) r.markHost.replaceChildren(it.mark);
      r.markHost.hidden = !it.mark;
      tweenNumber(r.valueEl, it.value || 0, { format: o.format });
      r.fill.style.transform = `scaleX(${Math.max(0, Math.min(1, (it.value || 0) / max)).toFixed(4)})`;
      r.fill.classList.toggle('is-lead', o.accent === 'first' && idx === 0);
      if (el.children[idx] !== r.li) el.insertBefore(r.li, el.children[idx] || null);
    });
    for (const [id, r] of rows) if (!keep.has(id)) { r.li.remove(); rows.delete(id); }
  };
  el.update = (p = {}) => { if (p.items) o.items = p.items; if ('max' in p) o.max = p.max; render(o.items || []); };
  render(o.items || []);
  return el;
}

/**
 * Sparkline for table cells only: sparkline({ values, width = 88, height = 24, tone: 'neutral'|'accent' }) -> <svg>; update({ values }) tweens.
 */
export function sparkline(props = {}) {
  const w = props.width || 88, hgt = props.height || 24;
  const path = svg('path', { class: 'spark__line' });
  const dot = svg('circle', { class: 'spark__dot', r: 2 });
  const el = svg('svg', { class: ['spark', props.tone === 'accent' && 'is-accent'], width: w, height: hgt, viewBox: `0 0 ${w} ${hgt}`, 'aria-hidden': 'true' }, [path, dot]);
  let shown = null;
  const draw = (vals) => {
    shown = vals;
    const n = vals.length;
    if (!n) { path.setAttribute('d', ''); dot.setAttribute('r', 0); return; }
    const max = Math.max(...vals, 1e-9), min = Math.min(...vals, 0);
    const pad = 3;
    const pts = vals.map((v, i) => [pad + ((w - 2 * pad) * (n === 1 ? 0.5 : i / (n - 1))), hgt - pad - ((hgt - 2 * pad) * (v - min)) / (max - min || 1)]);
    path.setAttribute('d', smoothPath(pts));
    dot.setAttribute('cx', pts[n - 1][0]); dot.setAttribute('cy', pts[n - 1][1]); dot.setAttribute('r', 2);
  };
  el.update = (p = {}) => {
    const next = (p.values || []).map((v) => v || 0);
    if (shown && shown.length === next.length) tweenValues(shown, next, draw, { duration: 300 }); else draw(next);
  };
  draw((props.values || []).map((v) => v || 0));
  return el;
}

/**
 * Horizontal meter for CPU, RAM, VRAM: meter({ label, value, max = 100, display, hint, warn = 70, err = 90, tone: 'neutral'|'accent' })
 * Fill is neutral (or accent), turns warn from 70 % and err from 90 %. node.update({ value, max, display, hint }).
 */
export function meter(props = {}) {
  const o = { max: 100, warn: 70, err: 90, tone: 'neutral', ...props };
  const labelEl = h('span.meter__label', o.label);
  const valueEl = h('span.meter__value');
  const hintEl = h('span.meter__hint');
  const fill = h('span.meter__fill');
  const track = h('span.meter__track', { role: 'meter', 'aria-label': o.label, 'aria-valuemin': 0 }, fill);
  const el = h('div', { class: ['meter', `meter--${o.tone}`] }, h('div.meter__head', labelEl, h('span.meter__right', hintEl, valueEl)), track);
  el.update = (p = {}) => {
    Object.assign(o, p);
    const known = typeof o.value === 'number' && Number.isFinite(o.value) && o.max > 0;
    const pct = known ? Math.max(0, Math.min(100, (o.value / o.max) * 100)) : 0;
    fill.style.transform = `scaleX(${(pct / 100).toFixed(4)})`;
    el.dataset.level = !known ? 'unknown' : pct >= o.err ? 'err' : pct >= o.warn ? 'warn' : 'ok';
    valueEl.textContent = o.display != null ? o.display : known ? fmtPercent(pct) : '—';
    hintEl.textContent = o.hint || '';
    hintEl.hidden = !o.hint;
    if (known) { track.setAttribute('aria-valuenow', Math.round(pct)); track.setAttribute('aria-valuemax', 100); }
    labelEl.textContent = o.label;
  };
  el.update({});
  return el;
}
