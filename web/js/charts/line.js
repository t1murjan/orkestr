import { h, svg } from '../core/dom.js';
import { tweenValues } from '../core/motion.js';
import { fmtInt, fmtTime } from '../core/format.js';
import { observeSize, niceScale, smoothPath, seriesColor, readout, compactAxis } from './util.js';

/**
 * Time-series line (optionally area) chart with hover crosshair and readout. One accent series, the rest neutral.
 * lineChart({ x: [ts...], series: [{ id, label, values: [...], color? }], height = 280, area = true, accent (series id, default first),
 *             format (readout values), formatAxis (y labels), formatX (x labels and readout title), yMax, ariaLabel })
 * -> node with update({ x, series, yMax }); tweens values when the shape is unchanged. Resizes with its container.
 */
export function lineChart(props = {}) {
  const o = { height: 280, area: true, format: fmtInt, formatAxis: compactAxis, formatX: fmtTime, ...props };
  const M = { l: 48, r: 12, t: 12, b: 26 };
  const gridG = svg('g.chart__grid');
  const axisG = svg('g.chart__axis');
  const areaP = svg('path.chart__area');
  const lineG = svg('g.chart__lines');
  const cross = svg('line.chart__cross');
  const dotG = svg('g.chart__dots');
  const hit = svg('rect.chart__hit', { fill: 'transparent' });
  const root = svg('svg', { class: 'chart__svg', role: 'img', 'aria-label': o.ariaLabel || 'График', tabindex: 0 }, [gridG, axisG, areaP, lineG, cross, dotG, hit]);
  const ro = readout();
  const el = h('div', { class: 'chart chart--line', style: { height: o.height + 'px' } }, root, ro.el);
  const S = { w: 0, h: o.height, x: o.x || [], series: o.series || [], shown: null, max: 0, idx: null, tw: null };

  const plot = () => ({ x0: M.l, x1: S.w - M.r, y0: M.t, y1: S.h - M.b });
  const px = (i, p) => (S.x.length <= 1 ? (p.x0 + p.x1) / 2 : p.x0 + ((p.x1 - p.x0) * i) / (S.x.length - 1));

  const draw = (vals, max) => {
    if (!S.w) return;
    const p = plot();
    const py = (v) => p.y1 - ((p.y1 - p.y0) * (v || 0)) / (max || 1);
    S.shown = vals; S.max = max;
    // grid + y labels
    const gridKids = [], axisKids = [];
    for (let k = 0; k <= 4; k++) {
      const v = (max * k) / 4, y = py(v);
      gridKids.push(svg('line', { x1: p.x0, x2: p.x1, y1: y, y2: y, class: k === 0 ? 'is-base' : '' }));
      axisKids.push(svg('text', { x: p.x0 - 8, y: y + 4, 'text-anchor': 'end' }, o.formatAxis(v)));
    }
    // x labels
    const n = S.x.length;
    if (n) {
      const want = Math.max(2, Math.min(6, Math.floor((p.x1 - p.x0) / 96)));
      for (let k = 0; k < want; k++) {
        const i = want === 1 ? 0 : Math.round(((n - 1) * k) / (want - 1));
        const anchor = k === 0 ? 'start' : k === want - 1 ? 'end' : 'middle';
        axisKids.push(svg('text', { x: px(i, p), y: S.h - 6, 'text-anchor': anchor }, o.formatX(S.x[i])));
      }
    }
    gridG.replaceChildren(...gridKids);
    axisG.replaceChildren(...axisKids);
    // lines
    while (lineG.children.length > S.series.length) lineG.lastChild.remove();
    S.series.forEach((s, si) => {
      let path = lineG.children[si];
      if (!path) { path = svg('path', { class: 'chart__line' }); lineG.appendChild(path); }
      const pts = vals[si].map((v, i) => [px(i, p), py(v)]);
      path.setAttribute('d', smoothPath(pts));
      path.style.stroke = seriesColor(S.series, si, o.accent);
      path.classList.toggle('is-lead', seriesColor(S.series, si, o.accent) === 'var(--accent)');
    });
    // area under the accent (or first) series
    const lead = Math.max(0, S.series.findIndex((s, i) => seriesColor(S.series, i, o.accent) === 'var(--accent)'));
    if (o.area && S.series.length && n > 1) {
      const pts = vals[lead].map((v, i) => [px(i, p), py(v)]);
      areaP.setAttribute('d', `${smoothPath(pts)}L${px(n - 1, p).toFixed(2)} ${p.y1}L${px(0, p).toFixed(2)} ${p.y1}Z`);
    } else areaP.setAttribute('d', '');
    hit.setAttribute('x', p.x0); hit.setAttribute('y', p.y0);
    hit.setAttribute('width', Math.max(0, p.x1 - p.x0)); hit.setAttribute('height', Math.max(0, p.y1 - p.y0));
    if (S.idx != null) showAt(S.idx);
  };

  const targetMax = () => {
    const all = S.series.flatMap((s) => s.values.filter((v) => v != null));
    const m = o.yMax != null ? o.yMax : Math.max(0, ...all);
    return o.yMax != null ? o.yMax : niceScale(m).max;
  };
  const targets = () => S.series.map((s) => s.values.map((v) => (v == null ? 0 : v)));

  const showAt = (i) => {
    const n = S.x.length;
    if (!n || !S.shown) return;
    S.idx = Math.max(0, Math.min(n - 1, i));
    const p = plot();
    const xx = px(S.idx, p);
    const py = (v) => p.y1 - ((p.y1 - p.y0) * (v || 0)) / (S.max || 1);
    cross.setAttribute('x1', xx); cross.setAttribute('x2', xx); cross.setAttribute('y1', p.y0); cross.setAttribute('y2', p.y1);
    cross.style.opacity = '1';
    dotG.replaceChildren(...S.series.map((s, si) => svg('circle', { class: 'chart__dot', cx: xx, cy: py(S.shown[si][S.idx]), r: 3.5, style: { stroke: seriesColor(S.series, si, o.accent) } })));
    const rows = S.series.map((s, si) => ({ label: s.label, value: o.format(s.values[S.idx]), color: seriesColor(S.series, si, o.accent) }));
    const first = S.shown.length ? py(Math.max(...S.shown.map((v) => v[S.idx] || 0))) : p.y0;
    ro.show(o.formatX(S.x[S.idx]), rows, xx, first, { width: S.w, height: S.h });
  };
  const hideHover = () => { S.idx = null; cross.style.opacity = '0'; dotG.replaceChildren(); ro.hide(); };
  hit.addEventListener('pointermove', (e) => {
    const r = root.getBoundingClientRect(), p = plot();
    const f = (e.clientX - r.left - p.x0) / Math.max(1, p.x1 - p.x0);
    showAt(Math.round(f * (S.x.length - 1)));
  });
  hit.addEventListener('pointerleave', hideHover);
  root.addEventListener('blur', hideHover);
  root.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowLeft') { e.preventDefault(); showAt((S.idx ?? S.x.length) - 1); }
    else if (e.key === 'ArrowRight') { e.preventDefault(); showAt((S.idx ?? -1) + 1); }
    else if (e.key === 'Escape') hideHover();
  });

  const sameShape = (next) => S.series.length === next.series.length && S.x.length === next.x.length
    && S.series.every((s, i) => s.id === next.series[i].id);

  el.update = (p = {}) => {
    const next = { x: p.x ?? S.x, series: p.series ?? S.series };
    if ('yMax' in p) o.yMax = p.yMax;
    const animate = S.shown && sameShape(next);
    const from = S.shown, fromMax = S.max;
    S.x = next.x; S.series = next.series;
    if (S.tw) S.tw.cancel();
    const to = targets(), toMax = targetMax();
    if (!animate || !from) { draw(to, toMax); return; }
    const flatFrom = from.flat(), flatTo = to.flat();
    S.tw = tweenValues([...flatFrom, fromMax], [...flatTo, toMax], (cur) => {
      const sz = S.x.length;
      draw(S.series.map((_, si) => cur.slice(si * sz, (si + 1) * sz)), cur[cur.length - 1]);
    });
  };

  root.setAttribute('width', '100%');
  root.setAttribute('height', o.height);
  observeSize(el, ({ width }) => {
    S.w = width;
    root.setAttribute('viewBox', `0 0 ${width} ${S.h}`);
    draw(S.shown || targets(), S.max || targetMax());
  });
  return el;
}
