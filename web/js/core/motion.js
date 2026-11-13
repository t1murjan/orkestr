// Motion helpers. One curve, meaningful motion only (docs/DESIGN.md "Motion").
export const EASE_CSS = 'cubic-bezier(0.22, 1, 0.36, 1)';

const mq = typeof matchMedia === 'function' ? matchMedia('(prefers-reduced-motion: reduce)') : null;
/** True when the user asked for less motion: skip tweens and loops, show end states. */
export const reducedMotion = () => !!(mq && mq.matches);

function cubicBezier(x1, y1, x2, y2) {
  const cx = 3 * x1, bx = 3 * (x2 - x1) - cx, ax = 1 - cx - bx;
  const cy = 3 * y1, by = 3 * (y2 - y1) - cy, ay = 1 - cy - by;
  const sx = (t) => ((ax * t + bx) * t + cx) * t;
  const sy = (t) => ((ay * t + by) * t + cy) * t;
  const dx = (t) => (3 * ax * t + 2 * bx) * t + cx;
  return (x) => {
    if (x <= 0) return 0;
    if (x >= 1) return 1;
    let t = x;
    for (let i = 0; i < 6; i++) {
      const e = sx(t) - x;
      if (Math.abs(e) < 1e-5) return sy(t);
      const d = dx(t);
      if (Math.abs(d) < 1e-6) break;
      t -= e / d;
    }
    let lo = 0, hi = 1;
    t = x;
    for (let i = 0; i < 20; i++) {
      const e = sx(t);
      if (Math.abs(e - x) < 1e-5) break;
      if (x > e) lo = t; else hi = t;
      t = (lo + hi) / 2;
    }
    return sy(t);
  };
}
/** The product curve as a function: ease(0..1) -> 0..1 */
export const ease = cubicBezier(0.22, 1, 0.36, 1);

/** Generic tween. onUpdate(eased 0..1). Returns { cancel() }. Reduced motion jumps to the end. */
export function tween({ duration = 300, onUpdate, onDone, delay = 0 }) {
  if (reducedMotion() || duration <= 0) {
    onUpdate(1);
    if (onDone) onDone();
    return { cancel() {} };
  }
  let raf = 0, cancelled = false, start = 0;
  const step = (now) => {
    if (cancelled) return;
    if (!start) start = now + delay;
    const t = (now - start) / duration;
    if (t < 0) { raf = requestAnimationFrame(step); return; }
    if (t >= 1) { onUpdate(1); if (onDone) onDone(); return; }
    onUpdate(ease(t));
    raf = requestAnimationFrame(step);
  };
  raf = requestAnimationFrame(step);
  return { cancel() { cancelled = true; cancelAnimationFrame(raf); } };
}

/** Tween a number inside an element: tweenNumber(el, 1240, { format: fmtInt }). Remembers the shown value on el. */
export function tweenNumber(el, to, { format = (n) => String(Math.round(n)), duration = 300, from } = {}) {
  if (el.__tw) el.__tw.cancel();
  const start = from != null ? from : (typeof el.__num === 'number' ? el.__num : null);
  if (typeof to !== 'number' || !Number.isFinite(to)) {
    el.__num = null;
    el.textContent = format(to);
    return;
  }
  if (start == null || start === to) {
    el.__num = to;
    el.textContent = format(to);
    return;
  }
  el.__tw = tween({
    duration,
    onUpdate: (e) => {
      const v = start + (to - start) * e;
      el.__num = v;
      el.textContent = format(v);
    },
    onDone: () => { el.__num = to; el.textContent = format(to); el.__tw = null; },
  });
}

/** Tween an array of numbers: tweenValues(from, to, fn(currentArray), opts) -> { cancel() } */
export function tweenValues(from, to, onUpdate, { duration = 360 } = {}) {
  if (!from || from.length !== to.length) { onUpdate(to); return { cancel() {} }; }
  return tween({
    duration,
    onUpdate: (e) => onUpdate(to.map((v, i) => (v == null || from[i] == null ? v : from[i] + (v - from[i]) * e))),
  });
}

/** One entrance on load: children fade and rise with a small stagger (40-60 ms, at most six). */
export function stagger(nodes, { delay = 50, rise = 8, duration = 360, max = 6 } = {}) {
  const list = Array.from(nodes).filter(Boolean);
  if (reducedMotion()) return;
  list.forEach((n, i) => {
    if (!n.animate) return;
    n.animate(
      [{ opacity: 0, transform: `translateY(${rise}px)` }, { opacity: 1, transform: 'translateY(0)' }],
      { duration, delay: Math.min(i, max) * delay, easing: EASE_CSS, fill: 'backwards' },
    );
  });
}

/** Fade and rise a floating element from its origin. dir: 'up' (comes from below), 'down', 'left', 'right'. */
export function popIn(el, { dir = 'up', distance = 6, duration = 200 } = {}) {
  if (reducedMotion() || !el.animate) return null;
  const t = { up: `translateY(${distance}px)`, down: `translateY(${-distance}px)`, left: `translateX(${distance}px)`, right: `translateX(${-distance}px)` }[dir];
  return el.animate([{ opacity: 0, transform: t }, { opacity: 1, transform: 'none' }], { duration, easing: EASE_CSS });
}
export function popOut(el, { duration = 150 } = {}) {
  if (reducedMotion() || !el.animate) return Promise.resolve();
  const a = el.animate([{ opacity: 1 }, { opacity: 0 }], { duration, easing: EASE_CSS, fill: 'forwards' });
  return a.finished.catch(() => {});
}

/**
 * Move a mark along an SVG path (a delegation travelling down a link).
 * mark: an SVG element, positioned with a transform; its own coordinates should be centred on 0,0.
 * opts: { duration=1400, loop=true, reverse=false, onEnd }. Returns { stop() }.
 * With reduced motion the mark is parked in the middle of the path and nothing loops.
 */
export function travel(path, mark, { duration = 1400, loop = true, reverse = false, onEnd } = {}) {
  const len = path.getTotalLength();
  const place = (f) => {
    const p = path.getPointAtLength(len * (reverse ? 1 - f : f));
    mark.setAttribute('transform', `translate(${p.x.toFixed(2)} ${p.y.toFixed(2)})`);
  };
  if (reducedMotion()) { place(0.5); return { stop() {} }; }
  let raf = 0, stopped = false, t0 = 0;
  const frame = (now) => {
    if (stopped) return;
    if (!t0) t0 = now;
    let f = (now - t0) / duration;
    if (f >= 1) {
      if (!loop) { place(1); if (onEnd) onEnd(); return; }
      t0 = now; f = 0;
    }
    place(ease(Math.max(0, f)));
    raf = requestAnimationFrame(frame);
  };
  place(0);
  raf = requestAnimationFrame(frame);
  return { stop() { stopped = true; cancelAnimationFrame(raf); } };
}

/** A new link draws itself in once: animates stroke-dashoffset from full length to 0. */
export function drawIn(path, { duration = 360 } = {}) {
  if (reducedMotion() || !path.animate) return;
  const len = path.getTotalLength();
  path.style.strokeDasharray = String(len);
  const a = path.animate([{ strokeDashoffset: len }, { strokeDashoffset: 0 }], { duration, easing: EASE_CSS });
  a.onfinish = () => { path.style.strokeDasharray = ''; };
}
