import { h, svg } from '../core/dom.js';
import { tweenValues } from '../core/motion.js';
import { fmtInt, fmtTime } from '../core/format.js';
import { observeSize, niceScale, seriesColor, readout, compactAxis } from './util.js';

/**
 * Stacked bars over time. One accent series (default: the first), the others on neutral steps.
 * stackedBars({ x: [ts...], series: [{ id, label, values }], height = 220, accent, format, formatAxis, formatX, ariaLabel })
 * Series are stacked bottom to top in the given order. node.update({ x, series }) tweens.
 */
export function stackedBars(props = {}) {
  const o = { height: 220, format: fmtInt, formatAxis: compactAxis, formatX: fmtTime, ...props };
  const M = { l: 48, r: 8, t: 12, b: 26 };
  const gridG = svg('g.chart__grid');
  const axisG = svg('g.chart__axis');
  const barsG = svg('g.chart__bars');
  const band = svg('rect.chart__band', { rx: 2 });
  const hit = svg('rect.chart__hit', { fill: 'transparent' });
  const root = svg('svg', { class: 'chart__svg', role: 'img', 'aria-label': o.ariaLabel || 'Столбчатая диаграмма', tabindex: 0 }, [gridG, band, barsG, axisG, hit]);
  const ro = readout();
  const el = h('div', { class: 'chart chart--bars', style: { height: o.height + 'px' } }, root, ro.el);
  const S = { w: 0, h: o.height, x: o.x || [], series: o.series || [], shown: null, max: 0, idx: null, tw: null };

  const plot = () => ({ x0: M.l, x1: S.w - M.r, y0: M.t, y1: S.h - M.b });
  const targets = () => S.series.map((s) => s.values.map((v) => v || 0));
  const targetMax = () => niceScale(Math.max(0, ...S.x.map((_, i) => S.series.reduce((a, s) => a + (s.values[i] || 0), 0)))).max;

  const draw = (vals, max) => {
    if (!S.w) return;
    const p = plot(), n = S.x.length;
    S.shown = vals; S.max = max;
    const py = (v) => p.y1 - ((p.y1 - p.y0) * v) / (max || 1);
    const gridKids = [], axisKids = [];
    for (let k = 0; k <= 4; k++) {
      const v = (max * k) / 4, y = py(v);
      gridKids.push(svg('line', { x1: p.x0, x2: p.x1, y1: y, y2: y, class: k === 0 ? 'is-base' : '' }));
      axisKids.push(svg('text', { x: p.x0 - 8, y: y + 4, 'text-anchor': 'end' }, o.formatAxis(v)));
    }
    const slot = n ? (p.x1 - p.x0) / n : 0;
    const bw = Math.max(2, Math.min(28, slot - Math.max(1, slot * 0.25)));
    if (n) {
      const want = Math.max(2, Math.min(6, Math.floor((p.x1 - p.x0) / 96)));
      for (let k = 0; k < want; k++) {
        const i = Math.round(((n - 1) * k) / (want - 1));
        const cx = p.x0 + slot * (i + 0.5);
        axisKids.push(svg('text', { x: cx, y: S.h - 6, 'text-anchor': k === 0 ? 'start' : k === want - 1 ? 'end' : 'middle' }, o.formatX(S.x[i])));
      }
    }
    gridG.replaceChildren(...gridKids);
    axisG.replaceChildren(...axisKids);
    const rects = [];
    for (let i = 0; i < n; i++) {
      let acc = 0;
      const cx = p.x0 + slot * (i + 0.5);
      S.series.forEach((s, si) => {
        const v = vals[si][i];
        if (v <= 0) return;
        const y1 = py(acc + v), y0 = py(acc);
        acc += v;
        rects.push(svg('rect', { class: 'chart__bar', x: (cx - bw / 2).toFixed(2), y: y1.toFixed(2), width: bw.toFixed(2), height: Math.max(0, y0 - y1 - 1).toFixed(2), style: { fill: seriesColor(S.series, si, o.accent) } }));
      });
    }
    barsG.replaceChildren(...rects);
    hit.setAttribute('x', p.x0); hit.setAttribute('y', p.y0); hit.setAttribute('width', Math.max(0, p.x1 - p.x0)); hit.setAttribute('height', Math.max(0, p.y1 - p.y0));
    if (S.idx != null) showAt(S.idx);
  };

  const showAt = (i) => {
    const n = S.x.length;
    if (!n || !S.shown) return;
    S.idx = Math.max(0, Math.min(n - 1, i));
    const p = plot(), slot = (p.x1 - p.x0) / n;
    band.setAttribute('x', p.x0 + slot * S.idx); band.setAttribute('width', slot);
    band.setAttribute('y', p.y0); band.setAttribute('height', p.y1 - p.y0);
    band.style.opacity = '1';
    const rows = S.series.map((s, si) => ({ label: s.label, value: o.format(s.values[S.idx]), color: seriesColor(S.series, si, o.accent) })).reverse();
    ro.show(o.formatX(S.x[S.idx]), rows, p.x0 + slot * (S.idx + 0.5), p.y0 + 28, { width: S.w, height: S.h });
  };
  const hideHover = () => { S.idx = null; band.style.opacity = '0'; ro.hide(); };
  hit.addEventListener('pointermove', (e) => {
    const r = root.getBoundingClientRect(), p = plot();
    showAt(Math.floor(((e.clientX - r.left - p.x0) / Math.max(1, p.x1 - p.x0)) * S.x.length));
  });
  hit.addEventListener('pointerleave', hideHover);
  root.addEventListener('blur', hideHover);
  root.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowLeft') { e.preventDefault(); showAt((S.idx ?? S.x.length) - 1); }
    else if (e.key === 'ArrowRight') { e.preventDefault(); showAt((S.idx ?? -1) + 1); }
    else if (e.key === 'Escape') hideHover();
  });

  el.update = (p = {}) => {
    const next = { x: p.x ?? S.x, series: p.series ?? S.series };
    const same = S.shown && S.series.length === next.series.length && S.x.length === next.x.length && S.series.every((s, i) => s.id === next.series[i].id);
    const from = S.shown, fromMax = S.max;
    S.x = next.x; S.series = next.series;
    if (S.tw) S.tw.cancel();
    const to = targets(), toMax = targetMax();
    if (!same) { draw(to, toMax); return; }
    const sz = S.x.length;
    S.tw = tweenValues([...from.flat(), fromMax], [...to.flat(), toMax], (cur) => {
      draw(S.series.map((_, si) => cur.slice(si * sz, (si + 1) * sz)), cur[cur.length - 1]);
    });
  };

  root.setAttribute('width', '100%'); root.setAttribute('height', o.height);
  observeSize(el, ({ width }) => { S.w = width; root.setAttribute('viewBox', `0 0 ${width} ${S.h}`); draw(S.shown || targets(), S.max || targetMax()); });
  return el;
}
