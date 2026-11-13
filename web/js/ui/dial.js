// The context dial: the product's signature. A thin ring gauge with fine tick marks that shows how full an
// agent's context window is. Appears wherever an agent appears, and nowhere else.
import { h, svg } from '../core/dom.js';
import { ctxPercent, fmtInt, fmtPercent, NB } from '../core/format.js';
import { tweenNumber, reducedMotion, EASE_CSS } from '../core/motion.js';

// Geometry per size: ring stroke, tick length, gap between ticks and ring, minor ticks.
const GEOMETRY = {
  20: { sw: 2, tick: 0, gap: 0, minor: false },
  32: { sw: 2.5, tick: 2, gap: 2, minor: false },
  56: { sw: 3, tick: 3, gap: 2.5, minor: true },
  88: { sw: 4, tick: 4.5, gap: 3.5, minor: true },
};
const nearest = (size) => [20, 32, 56, 88].reduce((a, b) => (Math.abs(b - size) < Math.abs(a - size) ? b : a));

export const dialLevel = (pct) => (pct == null ? 'unknown' : pct >= 90 ? 'err' : pct >= 70 ? 'warn' : 'ok');

/**
 * contextDial({ used, window, size = 32, center, initials })
 *   size: 20 | 32 | 56 | 88
 *   center (56 and 88 only): 'initials' | 'percent' | Node | null ; initials: text for 'initials'
 *   unknown used/window renders an empty track, never 0 %.
 * -> node with update({ used, window, center, initials }) and node.percent (number | null)
 */
export function contextDial(props = {}) {
  const size = nearest(props.size || 32);
  const g = GEOMETRY[size];
  const half = size / 2;
  const outer = half - 0.5;
  const tickIn = outer - g.tick;
  const rRing = (g.tick ? tickIn - g.gap : outer) - g.sw / 2;
  const circ = 2 * Math.PI * rRing;

  const track = svg('circle', { class: 'dial__track', cx: half, cy: half, r: rRing, 'stroke-width': g.sw });
  const arc = svg('circle', {
    class: 'dial__arc', cx: half, cy: half, r: rRing, 'stroke-width': g.sw,
    'stroke-dasharray': circ.toFixed(3), 'stroke-dashoffset': circ.toFixed(3), transform: `rotate(-90 ${half} ${half})`,
  });
  const ticks = [];
  if (g.tick) {
    for (let i = 0; i < 20; i++) {
      const minor = i % 2 === 1;
      if (minor && !g.minor) continue;
      const pctAt = i * 5;
      const a = (pctAt / 100) * Math.PI * 2 - Math.PI / 2;
      const len = minor ? g.tick * 0.55 : g.tick;
      const r1 = outer, r2 = outer - len;
      const line = svg('line', {
        class: 'dial__tick' + (minor ? ' is-minor' : ''),
        x1: (half + Math.cos(a) * r1).toFixed(2), y1: (half + Math.sin(a) * r1).toFixed(2),
        x2: (half + Math.cos(a) * r2).toFixed(2), y2: (half + Math.sin(a) * r2).toFixed(2),
      });
      line.__at = pctAt;
      ticks.push(line);
    }
  }
  const drawing = svg('svg', { class: 'dial__svg', width: size, height: size, viewBox: `0 0 ${size} ${size}`, 'aria-hidden': 'true' }, [track, ...ticks, arc]);
  const centerEl = size >= 56 ? h('span.dial__center') : null;
  const el = h('span', { class: `dial dial--${size}`, role: 'img', style: { '--dial-size': size + 'px' } }, drawing, centerEl);

  const state = { used: null, window: null, center: props.center ?? null, initials: props.initials ?? '', offset: circ, mounted: false };
  let pctEl = null, unitEl = null;

  const renderCenter = (pct) => {
    if (!centerEl) return;
    const c = state.center;
    if (c === 'percent') {
      if (!pctEl) {
        pctEl = h('span.dial__pct-n');
        unitEl = h('span.dial__pct-u', '%');
        centerEl.replaceChildren(h('span.dial__pct', pctEl, unitEl));
      }
      unitEl.hidden = pct == null;
      pctEl.parentNode.classList.toggle('is-3', pct != null && Math.round(pct) >= 100);
      tweenNumber(pctEl, pct == null ? null : Math.round(pct), { format: (n) => (n == null ? '—' : String(Math.round(n))), duration: 300 });
    } else if (c === 'initials') {
      pctEl = null;
      centerEl.replaceChildren(h('span.dial__initials', state.initials || ''));
    } else if (c instanceof Node) {
      pctEl = null;
      centerEl.replaceChildren(c);
    } else { pctEl = null; centerEl.replaceChildren(); }
  };

  const apply = (animate) => {
    const pct = ctxPercent(state.used, state.window);
    const clamped = pct == null ? null : Math.max(0, Math.min(100, pct));
    const level = dialLevel(pct);
    el.dataset.level = level;
    el.percent = pct;
    const target = circ * (1 - (clamped || 0) / 100);
    let from = state.offset;
    if (el.isConnected) {
      const cur = parseFloat(getComputedStyle(arc).strokeDashoffset);
      if (Number.isFinite(cur)) from = cur;
    }
    arc.style.strokeDashoffset = target.toFixed(3);
    arc.style.opacity = clamped == null ? '0' : '1';
    arc.getAnimations().forEach((a) => a.cancel());
    if (animate && !reducedMotion() && arc.animate && from !== target && clamped != null) {
      arc.animate([{ strokeDashoffset: from }, { strokeDashoffset: target }], { duration: 400, easing: EASE_CSS });
    }
    state.offset = target;
    for (const t of ticks) t.classList.toggle('is-lit', clamped != null && clamped > 0 && t.__at <= clamped + 0.001);
    if (clamped == null) el.setAttribute('aria-label', 'Контекст неизвестен');
    else el.setAttribute('aria-label', `Контекст заполнен на ${fmtPercent(pct)}, ${fmtInt(state.used)} из ${fmtInt(state.window)} токенов`.replace(new RegExp(NB, 'g'), ' '));
    renderCenter(clamped == null ? null : pct);
  };

  el.update = (p = {}) => {
    if ('used' in p) state.used = p.used;
    if ('window' in p) state.window = p.window;
    if ('center' in p) state.center = p.center;
    if ('initials' in p) state.initials = p.initials;
    apply(true);
  };
  state.used = props.used ?? null;
  state.window = props.window ?? null;
  // first draw: the arc grows in from empty (the offset starts full)
  apply(true);
  return el;
}
