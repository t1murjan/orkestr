// Client cache: /state once, then kept fresh from live events.
//   store.get('agents' | 'edges' | 'runtimes' | 'servers' | 'settings' | 'hardware' | 'hardwareSeries' | 'meta' | 'connection' | 'loaded')
//   store.subscribe(key, fn) -> off      fn(value, key); store.watch(key, fn) also calls fn once now
// Runs and steps live in a small cache of recently seen ones (see the run helpers below).
import { api } from './api.js';
import { live } from './live.js';

const state = {
  agents: [],
  edges: [],
  runtimes: [],
  servers: [],
  settings: null,
  hardware: null,
  hardwareSeries: [],
  meta: { version: null, demo_seeded: false },
  connection: 'connecting', // 'connecting' | 'open' | 'closed'
  loaded: false,
  loadError: null,
};

const HW_MAX = 300;
const RUN_CAP = 400;
const subs = new Map();      // key -> Set(fn)
const pending = new Set();   // keys with a scheduled notification
let agentIndex = new Map();

const runs = new Map();          // id -> Run
const steps = new Map();         // runId -> Map(stepId -> Step)
const runSubs = new Map();       // runId -> Set(fn)
const treeSubs = new Map();      // rootRunId -> Set(fn)
const runsSubs = new Set();      // any run change
const pendingRuns = { ids: new Set(), roots: new Set(), list: false };

function emit(key) {
  pending.add(key);
  queueMicrotask(flush);
}
function flush() {
  if (pending.size) {
    const keys = [...pending];
    pending.clear();
    for (const k of keys) for (const fn of [...(subs.get(k) || [])]) { try { fn(state[k], k); } catch (e) { console.error(e); } }
  }
  if (pendingRuns.ids.size || pendingRuns.roots.size || pendingRuns.list) {
    const ids = [...pendingRuns.ids], roots = [...pendingRuns.roots], list = pendingRuns.list;
    pendingRuns.ids.clear(); pendingRuns.roots.clear(); pendingRuns.list = false;
    for (const id of ids) for (const fn of [...(runSubs.get(id) || [])]) { try { fn(runSnapshot(id), id); } catch (e) { console.error(e); } }
    for (const id of roots) for (const fn of [...(treeSubs.get(id) || [])]) { try { fn(treeSnapshot(id), id); } catch (e) { console.error(e); } }
    if (list) for (const fn of [...runsSubs]) { try { fn(store.recentRuns()); } catch (e) { console.error(e); } }
  }
}
function setKey(key, value) { state[key] = value; emit(key); }

function reindex() { agentIndex = new Map(state.agents.map((a) => [a.id, a])); }
function upsert(list, item) {
  const i = list.findIndex((x) => x.id === item.id);
  if (i < 0) return [...list, item];
  const next = list.slice();
  next[i] = item;
  return next;
}

// ---- runs and steps -------------------------------------------------------
const sortSteps = (m) => [...m.values()].sort((a, b) => a.seq - b.seq);
function runSnapshot(id) { return { run: runs.get(id) || null, steps: steps.has(id) ? sortSteps(steps.get(id)) : [] }; }
function treeSnapshot(rootId) {
  const list = [...runs.values()].filter((r) => r.root_run_id === rootId || r.id === rootId)
    .sort((a, b) => (a.depth - b.depth) || ((a.started_at || 0) - (b.started_at || 0)));
  return { runs: list, steps: list.flatMap((r) => (steps.has(r.id) ? sortSteps(steps.get(r.id)) : [])) };
}
function touchRun(runId) {
  pendingRuns.ids.add(runId);
  const r = runs.get(runId);
  pendingRuns.roots.add(r ? (r.root_run_id || r.id) : runId);
  pendingRuns.list = true;
  queueMicrotask(flush);
}
function evictRuns() {
  if (runs.size <= RUN_CAP) return;
  const watched = new Set([...runSubs.keys(), ...treeSubs.keys()]);
  const victims = [...runs.values()]
    .filter((r) => !watched.has(r.id) && !watched.has(r.root_run_id) && r.status !== 'running')
    .sort((a, b) => (a.started_at || 0) - (b.started_at || 0));
  for (const r of victims.slice(0, runs.size - RUN_CAP)) { runs.delete(r.id); steps.delete(r.id); }
}
function ingestRun(run) {
  if (!run || !run.id) return;
  runs.set(run.id, run);
  touchRun(run.id);
  evictRuns();
}
function ingestStep(step) {
  if (!step || !step.run_id) return;
  if (!steps.has(step.run_id)) steps.set(step.run_id, new Map());
  steps.get(step.run_id).set(step.id, step);
  touchRun(step.run_id);
}

// ---- loading ---------------------------------------------------------------
let loading = null;
async function load() {
  if (loading) return loading;
  loading = (async () => {
    try {
      const s = await api.get('/state');
      state.agents = s.agents || [];
      reindex();
      state.edges = s.edges || [];
      state.runtimes = s.runtimes || [];
      state.servers = s.servers || [];
      state.settings = s.settings || null;
      state.hardware = s.hardware || null;
      state.meta = { version: s.version || null, demo_seeded: !!s.demo_seeded };
      if (s.hardware) pushHardware(s.hardware, false);
      state.loaded = true;
      state.loadError = null;
      for (const k of ['agents', 'edges', 'runtimes', 'servers', 'settings', 'hardware', 'meta', 'loaded', 'loadError', 'hardwareSeries']) emit(k);
    } catch (e) {
      state.loadError = e;
      emit('loadError');
    } finally {
      loading = null;
    }
  })();
  return loading;
}

function pushHardware(sample, notify = true) {
  const series = state.hardwareSeries;
  if (series.length && series[series.length - 1].ts === sample.ts) return;
  state.hardwareSeries = [...series, sample].slice(-HW_MAX);
  if (notify) emit('hardwareSeries');
}

function wire() {
  live.onStatus((s) => {
    setKey('connection', s);
    if (s === 'open' && !state.loaded) load();
  });
  live.onResync(() => { load(); });
  live.on('agent', (a) => { state.agents = upsert(state.agents, a); reindex(); emit('agents'); });
  live.on('agent.deleted', ({ id }) => {
    state.agents = state.agents.filter((a) => a.id !== id);
    state.edges = state.edges.filter((e) => e.supervisor_id !== id && e.subordinate_id !== id);
    reindex(); emit('agents'); emit('edges');
  });
  live.on('edge', (e) => { state.edges = upsert(state.edges, e); emit('edges'); });
  live.on('edge.deleted', ({ id }) => { state.edges = state.edges.filter((e) => e.id !== id); emit('edges'); });
  live.on('server', (s) => { state.servers = upsert(state.servers, s); emit('servers'); });
  live.on('server.deleted', ({ id }) => { state.servers = state.servers.filter((s) => s.id !== id); emit('servers'); });
  live.on('hardware', (h) => { state.hardware = h; emit('hardware'); pushHardware(h); });
  live.on('runtimes', (d) => { state.runtimes = d.runtimes || []; emit('runtimes'); });
  live.on('settings', (s) => { state.settings = s; emit('settings'); });
  live.on('run', ingestRun);
  live.on('step', ingestStep);
  live.on('demo.cleared', () => {
    runs.clear(); steps.clear();
    pendingRuns.list = true;
    for (const id of runSubs.keys()) pendingRuns.ids.add(id);
    for (const id of treeSubs.keys()) pendingRuns.roots.add(id);
    queueMicrotask(flush);
    load();
  });
}

// ---- public API ------------------------------------------------------------
export const store = {
  /** Load /state and start following live events. Call once (main.js does). */
  start() { wire(); live.start(); return load(); },
  reload: load,
  get(key) { return state[key]; },
  subscribe(key, fn) {
    if (!subs.has(key)) subs.set(key, new Set());
    subs.get(key).add(fn);
    return () => subs.get(key).delete(fn);
  },
  /** subscribe + call fn once with the current value */
  watch(key, fn) { fn(state[key], key); return store.subscribe(key, fn); },

  agentById: (id) => agentIndex.get(id) || null,
  agentByName: (name) => state.agents.find((a) => a.name === name) || null,
  runtimeById: (id) => state.runtimes.find((r) => r.id === id) || null,
  subordinatesOf: (id) => state.edges.filter((e) => e.supervisor_id === id).map((e) => agentIndex.get(e.subordinate_id)).filter(Boolean),
  supervisorsOf: (id) => state.edges.filter((e) => e.subordinate_id === id).map((e) => agentIndex.get(e.supervisor_id)).filter(Boolean),
  /** agents nobody supervises */
  rootAgents: () => { const subs = new Set(state.edges.map((e) => e.subordinate_id)); return state.agents.filter((a) => !subs.has(a.id)); },
  edgeBetween: (supId, subId) => state.edges.find((e) => e.supervisor_id === supId && e.subordinate_id === subId) || null,
  /** Client-side check before drawing a link: null when allowed, else 'self_edge' | 'duplicate_edge' | 'cycle' */
  linkProblem(supId, subId) {
    if (supId === subId) return 'self_edge';
    if (store.edgeBetween(supId, subId)) return 'duplicate_edge';
    const seen = new Set([subId]);
    const queue = [subId];
    while (queue.length) {
      const cur = queue.pop();
      for (const e of state.edges) {
        if (e.supervisor_id !== cur || seen.has(e.subordinate_id)) continue;
        if (e.subordinate_id === supId) return 'cycle';
        seen.add(e.subordinate_id); queue.push(e.subordinate_id);
      }
    }
    return null;
  },
  /** Total running runs across agents (for the nav badge). */
  activeRunCount: () => state.agents.reduce((n, a) => n + ((a.live && a.live.active_runs) || 0), 0),

  // runs
  run: (id) => runs.get(id) || null,
  steps: (runId) => (steps.has(runId) ? sortSteps(steps.get(runId)) : []),
  /** runs seen recently, newest first */
  recentRuns: (limit = 100) => [...runs.values()].sort((a, b) => (b.started_at ?? Infinity) - (a.started_at ?? Infinity)).slice(0, limit),
  treeOf: (rootId) => treeSnapshot(rootId),
  /** fn({run, steps}) on every change of this run or its steps */
  subscribeRun(id, fn) {
    if (!runSubs.has(id)) runSubs.set(id, new Set());
    runSubs.get(id).add(fn);
    return () => runSubs.get(id).delete(fn);
  },
  /** fn({runs, steps}) on every change anywhere in the delegation tree of rootId */
  subscribeTree(rootId, fn) {
    if (!treeSubs.has(rootId)) treeSubs.set(rootId, new Set());
    treeSubs.get(rootId).add(fn);
    return () => treeSubs.get(rootId).delete(fn);
  },
  /** fn(runs[]) on any run change */
  subscribeRuns(fn) { runsSubs.add(fn); return () => runsSubs.delete(fn); },
  ingestRun, ingestStep,
  /** GET /runs/{id}, put the whole tree and steps into the cache. Returns {run, tree, steps}. */
  async loadRun(id) {
    const d = await api.get(`/runs/${encodeURIComponent(id)}`);
    for (const r of d.tree || []) runs.set(r.id, r);
    if (d.run) runs.set(d.run.id, d.run);
    const bucket = new Map();
    for (const s of d.steps || []) { if (!bucket.has(s.run_id)) bucket.set(s.run_id, new Map()); bucket.get(s.run_id).set(s.id, s); }
    for (const [rid, m] of bucket) steps.set(rid, m);
    touchRun(id);
    for (const r of d.tree || []) touchRun(r.id);
    return d;
  },
  /** GET /runs with filters (status, agent_id, origin, root_only, limit, before); cached, returns the list in server order. */
  async loadRuns(params) {
    const d = await api.get('/runs', params);
    for (const r of d.runs || []) runs.set(r.id, r);
    pendingRuns.list = true;
    for (const r of d.runs || []) pendingRuns.ids.add(r.id);
    queueMicrotask(flush);
    evictRuns();
    return d.runs || [];
  },
  /** Prefill hardwareSeries from /hardware (call once from the infra screen). */
  async loadHardwareHistory(windowS = 600) {
    const d = await api.get('/hardware', { window_s: windowS });
    const merged = new Map([...(d.samples || []), ...state.hardwareSeries].map((s) => [s.ts, s]));
    state.hardwareSeries = [...merged.values()].sort((a, b) => a.ts - b.ts).slice(-HW_MAX);
    if (d.latest && !state.hardware) state.hardware = d.latest;
    emit('hardwareSeries'); emit('hardware');
    return state.hardwareSeries;
  },
};
export default store;
