// SSE client for /api/v1/stream with reconnect, backoff and a silence watchdog.
//   live.on('run', fn) -> off     fn(data, type); live.on('*', fn) gets every event
//   live.onStatus(fn) -> off      fn('connecting' | 'open' | 'closed')
//   live.onResync(fn) -> off      fired after a reconnect: reload whatever you show
import { BASE } from './api.js';

const TYPES = ['hello', 'ping', 'agent', 'agent.deleted', 'edge', 'edge.deleted', 'run', 'step',
  'server', 'server.deleted', 'hardware', 'runtimes', 'settings', 'demo.cleared'];
const SILENCE_MS = 45000; // the server pings every 15 s
const BACKOFF = [1000, 2000, 4000, 8000, 15000, 30000];

const handlers = new Map();
const statusFns = new Set();
const resyncFns = new Set();
let es = null;
let status = 'closed';
let attempt = 0;
let retryTimer = 0;
let watchdog = 0;
let wasOpen = false;
let wanted = false;

function setStatus(s) {
  if (status === s) return;
  status = s;
  for (const fn of [...statusFns]) { try { fn(s); } catch (e) { console.error(e); } }
}

function dispatch(type, data) {
  for (const fn of [...(handlers.get(type) || []), ...(handlers.get('*') || [])]) {
    try { fn(data, type); } catch (e) { console.error(e); }
  }
}

function armWatchdog() {
  clearTimeout(watchdog);
  watchdog = setTimeout(() => { if (wanted) drop(); }, SILENCE_MS);
}

function drop() {
  clearTimeout(watchdog);
  if (es) { es.close(); es = null; }
  setStatus('closed');
  if (!wanted) return;
  const delay = BACKOFF[Math.min(attempt, BACKOFF.length - 1)] * (0.85 + Math.random() * 0.3);
  attempt++;
  clearTimeout(retryTimer);
  retryTimer = setTimeout(connect, delay);
}

function connect() {
  if (!wanted || es) return;
  setStatus('connecting');
  try { es = new EventSource(BASE + '/stream'); } catch (e) { drop(); return; }
  es.onopen = () => {
    attempt = 0;
    armWatchdog();
    setStatus('open');
    if (wasOpen) for (const fn of [...resyncFns]) { try { fn(); } catch (e) { console.error(e); } }
    wasOpen = true;
  };
  es.onerror = () => { drop(); };
  for (const t of TYPES) {
    es.addEventListener(t, (ev) => {
      armWatchdog();
      let data = null;
      try { data = ev.data ? JSON.parse(ev.data) : null; } catch (e) { return; }
      dispatch(t, data);
    });
  }
}

export const live = {
  start() { if (wanted) return; wanted = true; connect(); },
  stop() { wanted = false; clearTimeout(retryTimer); clearTimeout(watchdog); if (es) { es.close(); es = null; } setStatus('closed'); },
  on(type, fn) {
    if (!handlers.has(type)) handlers.set(type, new Set());
    handlers.get(type).add(fn);
    return () => handlers.get(type).delete(fn);
  },
  onStatus(fn) { statusFns.add(fn); return () => statusFns.delete(fn); },
  onResync(fn) { resyncFns.add(fn); return () => resyncFns.delete(fn); },
  get status() { return status; },
  get connected() { return status === 'open'; },
};
export default live;
