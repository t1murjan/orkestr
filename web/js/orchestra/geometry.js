// Orchestra canvas geometry. An agent is a round graph node: the context dial is the node body, a compact label hangs beneath it.
// (x, y) is the top-left corner of the node's box; the circle sits at the top centre of that box.
export const NODE_W = 176;
export const NODE_H = 152;
export const R = 46;             // node circle radius (dial 88 + 2 px border room)
export const ARROW = 9;          // arrowhead length
export const GRID = 24;
export const LAYER_GAP = 232;
export const SLOT_GAP = 232;

const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const r2 = (n) => Math.round(n * 100) / 100;
const COS45 = Math.SQRT1_2;

export const centerOf = (p) => ({ x: p.x + NODE_W / 2, y: p.y + R });
/** The port sits on the rim, lower right. */
export const portOf = (p) => { const c = centerOf(p); return { x: c.x + R * COS45, y: c.y + R * COS45 }; };

/** Link between two node circles: starts on the supervisor's rim, ends with an arrowhead on the subordinate's rim. */
export function linkGeometry(a, b) {
  const c1 = centerOf(a), c2 = centerOf(b);
  const dx = c2.x - c1.x, dy = c2.y - c1.y;
  const len = Math.hypot(dx, dy);
  if (len < 2 * R + 14) return { d: '', arrow: '', mid: { x: (c1.x + c2.x) / 2, y: (c1.y + c2.y) / 2 } };
  const ux = dx / len, uy = dy / len;
  const nx = -uy, ny = ux;
  const bow = clamp(len * 0.06, 6, 28);
  const p0 = { x: c1.x + ux * (R + 2), y: c1.y + uy * (R + 2) };
  const tip = { x: c2.x - ux * (R + 3), y: c2.y - uy * (R + 3) };
  const ctl = { x: (p0.x + tip.x) / 2 + nx * bow, y: (p0.y + tip.y) / 2 + ny * bow };
  // arrow follows the curve's end tangent
  let tx = tip.x - ctl.x, ty = tip.y - ctl.y;
  const tl = Math.hypot(tx, ty) || 1; tx /= tl; ty /= tl;
  const base = { x: tip.x - tx * ARROW, y: tip.y - ty * ARROW };
  const px = -ty, py = tx;
  return {
    d: `M${r2(p0.x)} ${r2(p0.y)}Q${r2(ctl.x)} ${r2(ctl.y)} ${r2(base.x)} ${r2(base.y)}`,
    arrow: `M${r2(base.x + px * 4.5)} ${r2(base.y + py * 4.5)}L${r2(base.x - px * 4.5)} ${r2(base.y - py * 4.5)}L${r2(tip.x)} ${r2(tip.y)}Z`,
    mid: { x: 0.25 * p0.x + 0.5 * ctl.x + 0.25 * base.x, y: 0.25 * p0.y + 0.5 * ctl.y + 0.25 * base.y },
  };
}

/** Draft link from a node's port to the cursor, or snapped onto a target's rim. */
export function draftGeometry(a, pt, snapPos) {
  if (snapPos) return linkGeometry(a, snapPos);
  const s = portOf(a);
  return { d: `M${r2(s.x)} ${r2(s.y)}L${r2(pt.x)} ${r2(pt.y)}`, arrow: '', mid: pt };
}

export function boundsOf(positions) {
  if (!positions.length) return null;
  let minx = Infinity, miny = Infinity, maxx = -Infinity, maxy = -Infinity;
  for (const p of positions) {
    minx = Math.min(minx, p.x); miny = Math.min(miny, p.y);
    maxx = Math.max(maxx, p.x + NODE_W); maxy = Math.max(maxy, p.y + NODE_H);
  }
  return { minx, miny, maxx, maxy };
}

export const snap = (v, step = 8) => Math.round(v / step) * step;

/**
 * Layered top-down layout. agents: [{id, x, y}], edges: [{supervisor_id, subordinate_id}] -> Map(id -> {x, y}).
 * Depth = longest path from a root; order by barycentre of parents; children centred under parents; result centred on the old centre.
 */
export function layeredLayout(agents, edges) {
  const ids = new Set(agents.map((a) => a.id));
  const sups = new Map(agents.map((a) => [a.id, []]));
  const subs = new Map(agents.map((a) => [a.id, []]));
  for (const e of edges) {
    if (!ids.has(e.supervisor_id) || !ids.has(e.subordinate_id)) continue;
    sups.get(e.subordinate_id).push(e.supervisor_id);
    subs.get(e.supervisor_id).push(e.subordinate_id);
  }
  const depth = new Map();
  const dep = (id, guard = 0) => {
    if (depth.has(id)) return depth.get(id);
    if (guard > 100) return 0;
    const d = sups.get(id).length ? 1 + Math.max(...sups.get(id).map((p) => dep(p, guard + 1))) : 0;
    depth.set(id, d);
    return d;
  };
  agents.forEach((a) => dep(a.id));
  const maxD = Math.max(0, ...depth.values());
  const byId = new Map(agents.map((a) => [a.id, a]));
  const layers = Array.from({ length: maxD + 1 }, () => []);
  for (const a of agents) layers[depth.get(a.id)].push(a.id);
  layers[0].sort((p, q) => {
    const lp = subs.get(p).length ? 0 : 1, lq = subs.get(q).length ? 0 : 1;
    return lp - lq || byId.get(p).x - byId.get(q).x || byId.get(p).y - byId.get(q).y;
  });
  const cx = new Map();
  const place = (ids_, desired) => {
    const want = ids_.map((id, i) => ({ id, w: desired(id, i) }));
    want.sort((p, q) => p.w - q.w || p.id.localeCompare(q.id));
    const xs = [];
    want.forEach((it, i) => { xs.push(i === 0 ? it.w : Math.max(it.w, xs[i - 1] + SLOT_GAP)); });
    const shift = want.reduce((s, it) => s + it.w, 0) / want.length - xs.reduce((s, x) => s + x, 0) / xs.length;
    want.forEach((it, i) => cx.set(it.id, xs[i] + shift));
    return want.map((w) => w.id);
  };
  const avg = (list) => list.reduce((s, id) => s + cx.get(id), 0) / list.length;
  // layer 0: slots in order, a small extra gap before the unlinked agents
  let slot = 0, seenIso = false;
  const order0 = layers[0].map((id) => { if (!subs.get(id).length && !seenIso && slot > 0) { slot += 0.5; seenIso = true; } return { id, x: slot++ * SLOT_GAP }; });
  order0.forEach((o) => cx.set(o.id, o.x));
  for (let d = 1; d <= maxD; d++) layers[d] = place(layers[d], (id, i) => (sups.get(id).length ? avg(sups.get(id).filter((p) => cx.has(p))) : i * SLOT_GAP));
  for (let d = maxD - 1; d >= 0; d--) {
    const linked = layers[d].filter((id) => subs.get(id).length);
    if (!linked.length) continue;
    const rest = layers[d].filter((id) => !subs.get(id).length);
    const placed = place(linked, (id) => avg(subs.get(id)));
    if (rest.length) {
      const last = Math.max(...placed.map((id) => cx.get(id)));
      rest.forEach((id, i) => cx.set(id, last + SLOT_GAP * (i + 1.5)));
    }
  }
  // centre the result on the old centre of the graph
  const old = boundsOf(agents.map((a) => ({ x: a.x, y: a.y })));
  const xsAll = [...cx.values()];
  const newMid = (Math.min(...xsAll) + Math.max(...xsAll)) / 2;
  const oldMid = old ? (old.minx + old.maxx - NODE_W) / 2 : 0;
  const top = old ? old.miny : 0;
  const out = new Map();
  for (const a of agents) out.set(a.id, { x: snap(cx.get(a.id) - newMid + oldMid), y: snap(top + depth.get(a.id) * LAYER_GAP) });
  return out;
}
