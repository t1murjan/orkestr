// Shared chart plumbing: sizing, scales, smooth paths, the hover readout.
import { h } from '../core/dom.js';
import { fmtInt } from '../core/format.js';

/** Call cb({width, height}) now (if measurable) and whenever the node resizes. Returns disconnect(). */
export function observeSize(node, cb) {
  let last = '';
  const fire = (w, hh) => { const k = `${Math.round(w)}x${Math.round(hh)}`; if (k !== last && w > 0) { last = k; cb({ width: w, height: hh }); } };
  const ro = typeof ResizeObserver === 'function' ? new ResizeObserver((entries) => {
    const r = entries[0].contentRect;
    fire(r.width, r.height);
  }) : null;
  if (ro) ro.observe(node);
  queueMicrotask(() => fire(node.clientWidth, node.clientHeight));
  return () => ro && ro.disconnect();
}

/** Four intervals with a round step: niceScale(83) -> { max: 100, step: 25 } */
export function niceScale(maxValue, intervals = 4) {
  if (!(maxValue > 0)) return { max: intervals, step: 1 };
  const raw = maxValue / intervals;
  const pow = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * pow).find((s) => s >= raw - 1e-9);
  return { max: step * intervals, step };
}

/** Monotone cubic path through points [[x,y],...] (no overshoot between samples). */
export function smoothPath(pts) {
  const n = pts.length;
  if (n === 0) return '';
  if (n === 1) return `M${pts[0][0].toFixed(2)} ${pts[0][1].toFixed(2)}`;
  const dx = [], m = [], t = [];
  for (let i = 0; i < n - 1; i++) { dx[i] = pts[i + 1][0] - pts[i][0]; m[i] = (pts[i + 1][1] - pts[i][1]) / (dx[i] || 1); }
  t[0] = m[0]; t[n - 1] = m[n - 2];
  for (let i = 1; i < n - 1; i++) t[i] = m[i - 1] * m[i] <= 0 ? 0 : (m[i - 1] + m[i]) / 2;
  for (let i = 0; i < n - 1; i++) {
    if (m[i] === 0) { t[i] = 0; t[i + 1] = 0; continue; }
    const a = t[i] / m[i], b = t[i + 1] / m[i], s = a * a + b * b;
    if (s > 9) { const k = 3 / Math.sqrt(s); t[i] = k * a * m[i]; t[i + 1] = k * b * m[i]; }
  }
  let d = `M${pts[0][0].toFixed(2)} ${pts[0][1].toFixed(2)}`;
  for (let i = 0; i < n - 1; i++) {
    const x0 = pts[i][0], y0 = pts[i][1], x1 = pts[i + 1][0], y1 = pts[i + 1][1], k = dx[i] / 3;
    d += `C${(x0 + k).toFixed(2)} ${(y0 + t[i] * k).toFixed(2)} ${(x1 - k).toFixed(2)} ${(y1 - t[i + 1] * k).toFixed(2)} ${x1.toFixed(2)} ${y1.toFixed(2)}`;
  }
  return d;
}

/** Colour of series i: the accent series gets the accent, the rest sit on neutral steps. */
export function seriesColor(series, i, accentId) {
  const s = series[i];
  if (s.color) return s.color;
  const isAccent = accentId != null ? s.id === accentId : i === 0;
  if (isAccent) return 'var(--accent)';
  const steps = ['var(--ink-1)', 'var(--ink-2)', 'var(--ink-3)'];
  const rank = series.slice(0, i).filter((q, j) => !(accentId != null ? q.id === accentId : j === 0)).length;
  return steps[Math.min(rank, steps.length - 1)];
}

/** Floating readout inside a chart: title line and one row per series. Returns { el, show(title, rows, x, y, bounds), hide() }. */
export function readout() {
  const el = h('div.chart__readout', { role: 'presentation' });
  el.hidden = true;
  return {
    el,
    show(title, rows, x, y, bounds) {
      el.replaceChildren(h('div.chart__readout-title', title), ...rows.map((r) => h('div.chart__readout-row',
        h('span.chart__swatch', { style: { background: r.color } }), h('span.chart__readout-label', r.label), h('span.chart__readout-value', r.value))));
      el.hidden = false;
      const w = el.offsetWidth, hgt = el.offsetHeight;
      let left = x + 14;
      if (left + w > bounds.width - 4) left = x - 14 - w;
      left = Math.max(4, left);
      const top = Math.max(4, Math.min(y - hgt / 2, bounds.height - hgt - 4));
      el.style.transform = `translate(${Math.round(left)}px, ${Math.round(top)}px)`;
    },
    hide() { el.hidden = true; },
  };
}

export const compactAxis = (n) => {
  if (Math.abs(n) >= 1e6) return (n / 1e6).toFixed(n % 1e6 === 0 ? 0 : 1).replace('.', ',') + ' млн';
  if (Math.abs(n) >= 1e4) return Math.round(n / 1000) + ' тыс.';
  return fmtInt(n);
};
