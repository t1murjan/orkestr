// Pan and zoom for the canvas. The world layer is moved with one CSS transform; the dot grid follows through CSS variables.
import { tween } from '../core/motion.js';
import { GRID } from './geometry.js';

export const K_MIN = 0.25;
export const K_MAX = 2;
const clampK = (k) => Math.max(K_MIN, Math.min(K_MAX, k));

/**
 * createViewport(el, world, { onView, onBackgroundClick, onBackgroundDblClick(worldPt), onPanStart })
 * Anything inside the viewport marked [data-nopan] (nodes, links) is left alone.
 */
export function createViewport(el, world, opts = {}) {
  let v = { x: 0, y: 0, k: 1 };
  let anim = null;

  const apply = () => {
    world.style.transform = `translate(${v.x}px, ${v.y}px) scale(${v.k})`;
    const gs = GRID * v.k;
    el.style.setProperty('--gs', `${gs}px`);
    el.style.setProperty('--ik', (1 / v.k).toFixed(3));
    el.style.setProperty('--gx', `${v.x - gs / 2}px`);
    el.style.setProperty('--gy', `${v.y - gs / 2}px`);
    if (opts.onView) opts.onView(v);
  };
  const stopAnim = () => { if (anim) { anim.cancel(); anim = null; } };
  const rect = () => el.getBoundingClientRect();
  const toWorld = (cx, cy) => { const r = rect(); return { x: (cx - r.left - v.x) / v.k, y: (cy - r.top - v.y) / v.k }; };

  const setView = (next) => { stopAnim(); v = { x: next.x, y: next.y, k: clampK(next.k) }; apply(); };
  const zoomAt = (cx, cy, factor) => {
    stopAnim();
    const k = clampK(v.k * factor);
    const r = rect();
    const px = cx - r.left, py = cy - r.top;
    v = { k, x: px - (px - v.x) * (k / v.k), y: py - (py - v.y) * (k / v.k) };
    apply();
  };
  const animateTo = (target, duration = 360) => {
    stopAnim();
    const from = { ...v };
    anim = tween({ duration, onUpdate: (e) => { v = { x: from.x + (target.x - from.x) * e, y: from.y + (target.y - from.y) * e, k: from.k + (target.k - from.k) * e }; apply(); }, onDone: () => { anim = null; } });
  };

  // wheel: pan; with ctrl or cmd: zoom at the cursor
  const onWheel = (e) => {
    e.preventDefault();
    if (opts.onPanStart) opts.onPanStart();
    if (e.ctrlKey || e.metaKey) {
      const dy = e.deltaMode === 1 ? e.deltaY * 16 : e.deltaY;
      zoomAt(e.clientX, e.clientY, Math.exp(-Math.max(-40, Math.min(40, dy)) * 0.01));
    } else {
      stopAnim();
      const f = e.deltaMode === 1 ? 16 : 1;
      v = { ...v, x: v.x - e.deltaX * f, y: v.y - e.deltaY * f };
      apply();
    }
  };
  el.addEventListener('wheel', onWheel, { passive: false });

  // drag the background
  let pan = null;
  const onDown = (e) => {
    if (e.button !== 0 && e.button !== 1) return;
    if (e.target.closest('[data-nopan]')) return;
    stopAnim();
    pan = { id: e.pointerId, sx: e.clientX, sy: e.clientY, ox: v.x, oy: v.y, moved: false };
    el.setPointerCapture(e.pointerId);
  };
  const onMove = (e) => {
    if (!pan || e.pointerId !== pan.id) return;
    const dx = e.clientX - pan.sx, dy = e.clientY - pan.sy;
    if (!pan.moved && Math.hypot(dx, dy) < 4) return;
    if (!pan.moved) { pan.moved = true; el.classList.add('is-panning'); if (opts.onPanStart) opts.onPanStart(); }
    v = { ...v, x: pan.ox + dx, y: pan.oy + dy };
    apply();
  };
  const onUp = (e) => {
    if (!pan || e.pointerId !== pan.id) return;
    const moved = pan.moved;
    pan = null;
    el.classList.remove('is-panning');
    if (!moved && e.type === 'pointerup' && opts.onBackgroundClick) opts.onBackgroundClick(e);
  };
  const onDbl = (e) => {
    if (e.target.closest('[data-nopan]')) return;
    if (opts.onBackgroundDblClick) opts.onBackgroundDblClick(toWorld(e.clientX, e.clientY), e);
  };
  el.addEventListener('pointerdown', onDown);
  el.addEventListener('pointermove', onMove);
  el.addEventListener('pointerup', onUp);
  el.addEventListener('pointercancel', onUp);
  el.addEventListener('dblclick', onDbl);

  apply();
  return {
    get view() { return v; },
    toWorld,
    setView,
    zoomAt,
    zoomBy(f) { const r = rect(); zoomAt(r.left + r.width / 2, r.top + r.height / 2, f); },
    animateTo,
    /** Fit a world box into the viewport minus insets. */
    fitTo(box, { pad = 56, top = 0, bottom = 0, left = 0, right = 0, animate = true, maxK = 1 } = {}) {
      const r = rect();
      const aw = Math.max(80, r.width - left - right - pad * 2);
      const ah = Math.max(80, r.height - top - bottom - pad * 2);
      let target;
      if (!box) target = { x: r.width / 2, y: r.height / 2 - 40, k: 1 };
      else {
        const bw = box.maxx - box.minx, bh = box.maxy - box.miny;
        const k = clampK(Math.min(aw / bw, ah / bh, maxK));
        target = {
          k,
          x: left + pad + (aw - bw * k) / 2 - box.minx * k,
          y: top + pad + (ah - bh * k) / 2 - box.miny * k,
        };
      }
      if (animate) animateTo(target); else setView(target);
    },
    panBy(dx, dy) { stopAnim(); v = { ...v, x: v.x + dx, y: v.y + dy }; apply(); },
    destroy() {
      stopAnim();
      el.removeEventListener('wheel', onWheel);
      el.removeEventListener('pointerdown', onDown);
      el.removeEventListener('pointermove', onMove);
      el.removeEventListener('pointerup', onUp);
      el.removeEventListener('pointercancel', onUp);
      el.removeEventListener('dblclick', onDbl);
    },
  };
}
