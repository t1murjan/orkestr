// Which delegations are in flight right now, derived from live run and step events (and the runs already running when the page opened).
import { live } from '../core/live.js';

/**
 * createDelegationTracker(store, onChange)
 * onChange({ active: Map<linkKey, {sup, sub, n}>, returned: linkKey[] }) fires whenever the set changes.
 * A delegation is a running step of kind "delegation" in a running run; its link is parent agent -> child agent.
 */
export function createDelegationTracker(store, onChange) {
  let prev = new Map();           // stepId -> {key, sup, sub, childRunId, runId}
  let active = new Map();
  let offs = [];
  let stopped = false;
  const fetching = new Set();

  function compute({ quiet = false } = {}) {
    if (stopped) return;
    const next = new Map();
    for (const r of store.recentRuns(1000)) {
      if (r.status !== 'running' || !r.agent_id) continue;
      for (const s of store.steps(r.id)) {
        if (s.kind !== 'delegation' || s.status !== 'running' || !s.child_run_id) continue;
        const child = store.run(s.child_run_id);
        if (!child) {
          if (!fetching.has(s.child_run_id)) {
            fetching.add(s.child_run_id);
            store.loadRun(s.child_run_id).catch(() => {}).finally(() => { fetching.delete(s.child_run_id); compute(); });
          }
          continue;
        }
        if (!child.agent_id) continue;
        next.set(s.id, { key: `${r.agent_id}>${child.agent_id}`, sup: r.agent_id, sub: child.agent_id, childRunId: s.child_run_id, runId: r.id });
      }
    }
    const returned = [];
    if (!quiet) {
      for (const [id, info] of prev) {
        if (next.has(id)) continue;
        const step = store.steps(info.runId).find((x) => x.id === id);
        if (step && (step.status === 'ok' || step.status === 'error')) returned.push(info.key);
      }
    }
    const nextActive = new Map();
    for (const info of next.values()) {
      const cur = nextActive.get(info.key) || { sup: info.sup, sub: info.sub, n: 0 };
      cur.n += 1;
      nextActive.set(info.key, cur);
    }
    const changed = nextActive.size !== active.size || [...nextActive].some(([k, v]) => !active.has(k) || active.get(k).n !== v.n);
    prev = next;
    active = nextActive;
    if (changed || returned.length) onChange({ active, returned: [...new Set(returned)] });
  }

  /** Load what is running right now plus each running run's whole tree, then recompute. */
  async function bootstrap() {
    try {
      const running = await store.loadRuns({ status: 'running', limit: 200 });
      const runningIds = new Set(running.map((r) => r.id));
      // cached runs that still say "running" but were not returned have finished while we were away
      const stale = store.recentRuns(1000).filter((r) => r.status === 'running' && !runningIds.has(r.id)).map((r) => r.id);
      const roots = new Set(running.map((r) => r.root_run_id || r.id));
      for (const id of stale) roots.add(id);
      await Promise.all([...roots].slice(0, 40).map((id) => store.loadRun(id).catch(() => null)));
    } catch (e) { /* offline: the next resync retries */ }
    compute({ quiet: true });
  }

  return {
    start() {
      stopped = false;
      offs.push(store.subscribeRuns(() => compute()));
      offs.push(live.onResync(() => { bootstrap(); }));
      bootstrap();
    },
    refresh: bootstrap,
    get active() { return active; },
    stop() { stopped = true; offs.forEach((f) => f()); offs = []; },
  };
}
