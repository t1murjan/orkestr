// Hash router: #/  #/runs  #/runs/<id>  #/overview  #/infra  #/settings   (+ optional ?query)
//   router.current -> { view, path, params, query }
//   router.onChange(fn) -> off      fn(current), fired on every hash change incl. query-only changes
//   router.navigate('/runs/abc'), router.href('/runs'), router.setQuery({ status: 'error' })
const ROUTES = [
  { view: 'orchestra', pattern: '/' },
  { view: 'runs', pattern: '/runs' },
  { view: 'runs', pattern: '/runs/:id' },
  { view: 'overview', pattern: '/overview' },
  { view: 'infra', pattern: '/infra' },
  { view: 'settings', pattern: '/settings' },
];

const compiled = ROUTES.map((r) => {
  const keys = [];
  const rx = new RegExp('^' + r.pattern.replace(/:([a-z]+)/gi, (_, k) => { keys.push(k); return '([^/]+)'; }) + '/?$');
  return { ...r, rx, keys };
});

const fns = new Set();
let current = { view: 'orchestra', path: '/', params: {}, query: {} };

function parse(hash) {
  let raw = (hash || '').replace(/^#/, '') || '/';
  if (!raw.startsWith('/')) raw = '/' + raw;
  const qi = raw.indexOf('?');
  const path = qi >= 0 ? raw.slice(0, qi) : raw;
  const query = Object.fromEntries(new URLSearchParams(qi >= 0 ? raw.slice(qi + 1) : ''));
  for (const r of compiled) {
    const m = r.rx.exec(path);
    if (m) {
      const params = {};
      r.keys.forEach((k, i) => { params[k] = decodeURIComponent(m[i + 1]); });
      return { view: r.view, path, params, query };
    }
  }
  return null;
}

function sync() {
  const next = parse(location.hash);
  if (!next) { location.replace('#/'); return; }
  current = next;
  for (const fn of [...fns]) { try { fn(current); } catch (e) { console.error(e); } }
}

const buildQuery = (q) => {
  const s = new URLSearchParams(Object.entries(q).filter(([, v]) => v != null && v !== '')).toString();
  return s ? '?' + s : '';
};

export const router = {
  start() { window.addEventListener('hashchange', sync); sync(); },
  get current() { return current; },
  href: (path) => '#' + path,
  navigate(path, { replace = false } = {}) {
    const target = '#' + path;
    if (location.hash === target || (path === '/' && (location.hash === '' || location.hash === '#'))) return;
    if (replace) location.replace(target); else location.hash = target;
  },
  /** Update the query string of the current route without remounting the view. Pass null to remove a key. */
  setQuery(patch, { replace = true } = {}) {
    const q = { ...current.query, ...patch };
    const target = '#' + current.path + buildQuery(q);
    if (replace) location.replace(target); else location.hash = target;
  },
  onChange(fn) { fns.add(fn); return () => fns.delete(fn); },
};
export default router;
