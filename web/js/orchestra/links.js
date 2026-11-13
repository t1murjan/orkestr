// The link layer: supervisor -> subordinate paths with arrowheads, selection, delegation dashes, the link being drawn.
import { svg } from '../core/dom.js';
import { travel, drawIn, reducedMotion } from '../core/motion.js';
import { linkGeometry, draftGeometry } from './geometry.js';

const DASH_LEN = 8;

/** A travelling mark: wraps travel() so the dash also turns along the curve. */
function dashMark(kind) {
  const g = svg('g', { class: `link__dash${kind ? ' is-' + kind : ''}`, 'aria-hidden': 'true' }, svg('line', { x1: -DASH_LEN, y1: 0, x2: DASH_LEN, y2: 0 }));
  let prev = null, ang = kind === 'return' ? -90 : 90;
  const proxy = {
    setAttribute(name, val) {
      const m = /translate\(([-\d.]+) ([-\d.]+)\)/.exec(val);
      if (!m) return;
      const x = +m[1], y = +m[2];
      if (prev) {
        const dx = x - prev.x, dy = y - prev.y, dist = Math.hypot(dx, dy);
        if (dist > 0.4 && dist < 60) ang = (Math.atan2(dy, dx) * 180) / Math.PI;
      }
      prev = { x, y };
      g.setAttribute('transform', `translate(${x} ${y}) rotate(${ang.toFixed(1)})`);
    },
  };
  return { g, proxy, reset() { prev = null; } };
}

/**
 * createLinkLayer(svgEl, { onSelect(edgeId), onDelete(edgeId), label(edge) })
 *   render({ edges, ghosts, pos(id), selectedId, active:Set<key>, labelFor(sup, sub) })
 *   playReturn(key), setDraft(null | { from: {x,y}, to: {x,y}, snap?: {x,y} }), key(sup, sub)
 */
export function createLinkLayer(svgEl, { onSelect, onDelete } = {}) {
  const entries = new Map();   // key -> entry
  const draft = svg('g', { class: 'link link--draft', 'aria-hidden': 'true' }, svg('path', { class: 'link__path' }), svg('path', { class: 'link__arrow' }));
  const layer = svg('g', { class: 'links' });
  svgEl.append(layer, draft);
  draft.style.display = 'none';
  let first = true;
  let pending = [];

  const keyOf = (sup, sub) => `${sup}>${sub}`;

  function makeEntry(key, item, ghost) {
    const path = svg('path', { class: 'link__path' });
    const arrow = svg('path', { class: 'link__arrow' });
    const hit = svg('path', { class: 'link__hit', 'data-nopan': '' });
    const g = svg('g', { class: 'link' + (ghost ? ' link--ghost' : ''), 'data-nopan': '' }, hit, path, arrow);
    const entry = { key, g, path, arrow, hit, ghost, item, d: '', loop: null, mark: null, restart: 0, del: null };
    if (!ghost) {
      hit.setAttribute('tabindex', '0');
      hit.setAttribute('role', 'button');
      hit.addEventListener('click', (e) => { e.stopPropagation(); onSelect && onSelect(item.id); });
      hit.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onSelect && onSelect(item.id); }
      });
    }
    layer.appendChild(g);
    return entry;
  }

  function ensureDelete(entry, on, mid, label) {
    if (on && !entry.del) {
      const x = svg('g', { class: 'link__del', role: 'button', tabindex: '0', 'aria-label': `Удалить связь: ${label}`, 'data-nopan': '' },
        svg('circle', { r: 10 }),
        svg('path', { d: 'M-3.2 -3.2L3.2 3.2M3.2 -3.2L-3.2 3.2' }),
        svg('title', {}, 'Удалить связь'));
      x.addEventListener('click', (e) => { e.stopPropagation(); onDelete && onDelete(entry.item.id); });
      x.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); e.stopPropagation(); onDelete && onDelete(entry.item.id); } });
      x.addEventListener('pointerdown', (e) => e.stopPropagation());
      entry.g.appendChild(x);
      entry.del = x;
    } else if (!on && entry.del) { entry.del.remove(); entry.del = null; }
    if (entry.del) entry.del.setAttribute('transform', `translate(${mid.x} ${mid.y})`);
  }

  function stopLoop(entry) {
    if (entry.loop) { entry.loop.stop(); entry.loop = null; }
    if (entry.mark) { entry.mark.g.remove(); entry.mark = null; }
    clearTimeout(entry.restart);
  }
  function startLoop(entry) {
    stopLoop(entry);
    const mark = dashMark('live');
    entry.mark = mark;
    entry.g.appendChild(mark.g);
    entry.loop = travel(entry.path, mark.proxy, { duration: 1400, loop: true });
  }

  function render({ edges, ghosts, pos, selectedId, active, labelFor, focus }) {
    const seen = new Set();
    const items = [];
    for (const e of edges) items.push({ key: keyOf(e.supervisor_id, e.subordinate_id), sup: e.supervisor_id, sub: e.subordinate_id, item: e, ghost: false });
    for (const k of ghosts) if (!items.some((i) => i.key === k.key)) items.push({ key: k.key, sup: k.sup, sub: k.sub, item: { id: k.key }, ghost: true });
    for (const it of items) {
      const a = pos(it.sup), b = pos(it.sub);
      if (!a || !b) continue;
      seen.add(it.key);
      let entry = entries.get(it.key);
      if (entry && entry.ghost !== it.ghost) { stopLoop(entry); entry.g.remove(); entries.delete(it.key); entry = null; }
      const fresh = !entry;
      if (!entry) { entry = makeEntry(it.key, it.item, it.ghost); entries.set(it.key, entry); }
      entry.item = it.item;
      const geo = linkGeometry(a, b);
      const changed = geo.d !== entry.d;
      if (changed) {
        entry.d = geo.d;
        entry.path.setAttribute('d', geo.d);
        entry.hit.setAttribute('d', geo.d);
        entry.arrow.setAttribute('d', geo.arrow);
      }
      const isActive = active && active.has(it.key);
      const isSel = !it.ghost && selectedId === it.item.id;
      entry.g.classList.toggle('is-active', !!isActive);
      entry.g.classList.toggle('is-selected', isSel);
      const hot = !!focus && focus.ids.has(it.sup) && focus.ids.has(it.sub) && (!focus.id || it.sup === focus.id || it.sub === focus.id);
      entry.g.classList.toggle('is-hot', hot);
      entry.g.classList.toggle('is-dim', !!focus && !hot);
      if (!it.ghost) entry.hit.setAttribute('aria-label', `Связь: ${labelFor ? labelFor(it.sup, it.sub) : ''}`);
      ensureDelete(entry, isSel, geo.mid, labelFor ? labelFor(it.sup, it.sub) : '');
      if (isSel) layer.appendChild(entry.g); // keep the selected link on top
      if (fresh && !first && !it.ghost) drawIn(entry.path);
      // delegation dash: loop while active; follow the path when it moves
      if (isActive) {
        if (!entry.loop) startLoop(entry);
        else if (changed) {
          entry.mark.g.style.opacity = '0';
          clearTimeout(entry.restart);
          entry.restart = setTimeout(() => { if (entry.loop) { startLoop(entry); } }, 140);
        }
      } else if (entry.loop || entry.mark) stopLoop(entry);
    }
    for (const [key, entry] of entries) {
      if (seen.has(key)) continue;
      stopLoop(entry);
      entry.g.remove();
      entries.delete(key);
    }
    first = false;
  }

  /** One dash back up the link when the child run has finished. */
  function playReturn(key) {
    const entry = entries.get(key);
    if (!entry || reducedMotion()) return;
    const mark = dashMark('return');
    entry.g.appendChild(mark.g);
    const ctl = travel(entry.path, mark.proxy, {
      duration: 1000, loop: false, reverse: true,
      onEnd: () => { mark.g.remove(); pending = pending.filter((p) => p !== ctl); },
    });
    pending.push(ctl);
  }

  function setDraft(d) {
    if (!d) { draft.style.display = 'none'; return; }
    const geo = draftGeometry(d.from, d.to, d.snap);
    draft.style.display = '';
    draft.firstChild.setAttribute('d', geo.d);
    draft.lastChild.setAttribute('d', geo.arrow);
    draft.classList.toggle('is-snapped', !!d.snap);
  }

  return {
    render, playReturn, setDraft, key: keyOf,
    has: (key) => entries.has(key),
    destroy() { for (const e of entries.values()) stopLoop(e); pending.forEach((p) => p.stop()); entries.clear(); layer.remove(); draft.remove(); },
  };
}
