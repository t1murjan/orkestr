// Shared helpers of the runs, overview, infra and settings screens (my own files only).
import { svg } from '../core/dom.js';
import { fmtDuration } from '../core/format.js';

// Lucide paths for icons the shared set lacks (pause, trash, arrow-left, maximize, zoom).
const EXTRA = {
  pause: [['rect', { x: '14', y: '3', width: '5', height: '18', rx: '1' }], ['rect', { x: '5', y: '3', width: '5', height: '18', rx: '1' }]],
  'trash-2': [['path', { d: 'M3 6h18' }], ['path', { d: 'M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6' }], ['path', { d: 'M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2' }], ['line', { x1: '10', x2: '10', y1: '11', y2: '17' }], ['line', { x1: '14', x2: '14', y1: '11', y2: '17' }]],
  'arrow-left': [['path', { d: 'm12 19-7-7 7-7' }], ['path', { d: 'M19 12H5' }]],
  maximize: [['path', { d: 'M8 3H5a2 2 0 0 0-2 2v3' }], ['path', { d: 'M21 8V5a2 2 0 0 0-2-2h-3' }], ['path', { d: 'M3 16v3a2 2 0 0 0 2 2h3' }], ['path', { d: 'M16 21h3a2 2 0 0 0 2-2v-3' }]],
  'rotate-ccw': [['path', { d: 'M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8' }], ['path', { d: 'M3 3v5h5' }]],
};
export function xicon(name, size = 16) {
  return svg('svg', { class: 'icon', width: size, height: size, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': 1.5, 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true', focusable: 'false' },
    (EXTRA[name] || []).map(([t, a]) => svg(t, a)));
}

export const DOT = { queued: 'idle', running: 'working', ok: 'ok', error: 'error', cancelled: 'idle' };
export const isLive = (r) => r && (r.status === 'running' || r.status === 'queued');

/** Depth-first lane order of a delegation tree: parent, then its children by start time. */
export function laneOrder(runs) {
  const byParent = new Map();
  const ids = new Set(runs.map((r) => r.id));
  for (const r of runs) {
    const p = r.parent_run_id && ids.has(r.parent_run_id) ? r.parent_run_id : null;
    if (!byParent.has(p)) byParent.set(p, []);
    byParent.get(p).push(r);
  }
  const out = [];
  const walk = (p, depth) => {
    for (const r of (byParent.get(p) || []).sort((a, b) => (a.started_at || 0) - (b.started_at || 0))) {
      out.push({ run: r, depth });
      walk(r.id, depth + 1);
    }
  };
  walk(null, 0);
  return out;
}

/** Nice time tick step (ms) so that ticks are at least minPx apart. */
const STEPS = [10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 15000, 30000, 60000, 120000, 300000, 600000, 1800000, 3600000];
export function tickStep(msPerPx, minPx = 96) {
  const need = msPerPx * minPx;
  return STEPS.find((s) => s >= need) || STEPS[STEPS.length - 1];
}
export const fmtOffset = (ms) => (ms <= 0 ? '0 с' : fmtDuration(Math.round(ms)));

export const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

/** Pretty JSON for display. */
export function prettyJson(v) {
  try { return JSON.stringify(v, null, 2); } catch (e) { return String(v); }
}


/** Name to show for a run's agent: external sessions have no agent, the backend puts the task into agent_name. */
export function runAgentName(r) {
  if (!r) return '';
  if (r.origin === 'external' && !r.agent_id) {
    const n = r.agent_name || '', t = r.task || '';
    return !n || t.startsWith(n.replace(/…$/, '').slice(0, 24)) ? 'Сессия из терминала' : n;
  }
  return r.agent_name;
}
