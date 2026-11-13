// App shell: sidebar, top bar, routed content area. Boots the core modules and mounts the view for the current route.
import { h } from './core/dom.js';
import { api } from './core/api.js';
import { live } from './core/live.js';
import { store } from './core/store.js';
import { router } from './core/router.js';
import { stagger } from './core/motion.js';
import { icon, brandMark, runtimeMark, statusDot, tooltip, emptyState, overlayRoot } from './ui/index.js';
import { runtimeLabel } from './core/format.js';
import orchestra from './views/orchestra.js';
import runs from './views/runs.js';
import overview from './views/overview.js';
import infra from './views/infra.js';
import settings from './views/settings.js';

const VIEWS = { orchestra, runs, overview, infra, settings };
const NAV = [
  { view: 'orchestra', href: '/', label: 'Оркестр', icon: 'workflow' },
  { view: 'runs', href: '/runs', label: 'Запуски', icon: 'history' },
  { view: 'overview', href: '/overview', label: 'Обзор', icon: 'chart-no-axes-combined' },
  { view: 'infra', href: '/infra', label: 'Модели и железо', icon: 'cpu' },
  { view: 'settings', href: '/settings', label: 'Настройки', icon: 'settings' },
];

const collapsedMq = matchMedia('(max-width: 1099px)');

// ---- build the shell ---------------------------------------------------------
const navLinks = new Map();
const runsCount = h('span.nav__count');
const nav = h('nav.nav', { 'aria-label': 'Разделы' }, NAV.map((n) => {
  const a = h('a.nav__item', { href: router.href(n.href), 'aria-label': n.label },
    h('span.nav__icon', icon(n.icon)), h('span.nav__label', n.label), n.view === 'runs' ? runsCount : null);
  tooltip(a, () => (collapsedMq.matches ? n.label : ''), { placement: 'right', delay: 200 });
  navLinks.set(n.view, a);
  return a;
}));

const connDot = statusDot({ status: 'idle' });
const connText = h('span.conn__text', 'Подключение');
const connVer = h('span.conn__ver');
const rtsList = h('div.rts__list');
const sidebar = h('aside.sidebar',
  h('div.sidebar__brand', brandMark(20), h('span.wordmark', 'Оркестр')),
  nav,
  h('div.sidebar__foot',
    h('div.conn', { role: 'status' }, connDot, connText, connVer),
    h('div.rts', h('div.rts__title', 'Среда выполнения'), rtsList)));

const titleEl = h('h1', 'Оркестр');
const subEl = h('span.topbar__sub');
const actionsEl = h('div.topbar__actions');
const content = h('main.content', { id: 'content', tabindex: -1 });
const app = h('div.app', sidebar,
  h('div.main', h('header.topbar', h('div.topbar__title', titleEl, subEl), actionsEl), content));
document.body.append(h('a.skip-link', { href: '#content', onclick: (e) => { e.preventDefault(); content.focus(); } }, 'К содержимому'), app);
overlayRoot();

// ---- live sidebar state ------------------------------------------------------
store.watch('connection', (s) => {
  const label = s === 'open' ? 'На связи' : s === 'connecting' ? 'Подключение' : 'Нет связи';
  connDot.update({ status: s === 'open' ? 'ok' : s === 'connecting' ? 'idle' : 'error', label });
  connText.textContent = label;
});
store.watch('meta', (m) => { connVer.textContent = m && m.version ? 'v' + m.version : ''; });
store.watch('runtimes', (list) => {
  const rows = (list || []).map((r) => h('div', { class: ['rts__row', !r.available && 'is-missing'], title: r.error || null },
    runtimeMark({ runtime: r.id }), h('span.rts__label', r.label || runtimeLabel(r.id)),
    h('span.rts__ver', r.available ? (r.version || (r.id === 'demo' ? 'встроен' : 'найден')) : 'не найден')));
  rtsList.replaceChildren(...(rows.length ? rows : [h('div.rts__empty', store.get('connection') === 'open' ? 'Не обнаружена' : 'Нет данных')]));
});
store.watch('agents', () => {
  const n = store.activeRunCount();
  runsCount.replaceChildren(...(n > 0 ? [statusDot({ status: 'working', size: 6 }), String(n)] : []));
  runsCount.title = n > 0 ? 'Выполняется запусков: ' + n : '';
});

// ---- routing -----------------------------------------------------------------
let mounted = null; // { view, key }
let routeKey = '';

function makeCtx(route) {
  return {
    api, live, store, router,
    params: route.params, query: route.query,
    setTitle(title, sub = '') { titleEl.textContent = title; subEl.textContent = sub; document.title = `${title} — Оркестр`; },
    setActions(...nodes) { actionsEl.replaceChildren(...nodes.flat().filter(Boolean)); },
  };
}

function onRoute(route) {
  const view = VIEWS[route.view];
  navLinks.forEach((a, id) => { if (id === route.view) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current'); });
  const key = route.view + '|' + JSON.stringify(route.params);
  if (mounted && key === routeKey) return; // query-only change: views listen through router.onChange
  if (mounted && mounted.view === view && view.update && Object.keys(route.params).length) {
    routeKey = key;
    view.update(makeCtx(route));
    return;
  }
  if (mounted) { try { mounted.view.unmount(); } catch (e) { console.error(e); } }
  routeKey = key;
  const root = h('div', { class: `view view--${view.layout === 'canvas' ? 'canvas' : 'page'} view--${view.id}` });
  content.replaceChildren(root);
  content.scrollTop = 0;
  const ctx = makeCtx(route);
  ctx.setTitle(view.title);
  ctx.setActions();
  mounted = { view };
  try {
    view.mount(root, ctx);
  } catch (e) {
    console.error(e);
    root.replaceChildren(emptyState({ title: 'Экран не открылся', text: e && e.message ? e.message : 'Неизвестная ошибка' }));
  }
  if (view.layout !== 'canvas') stagger(root.children);
}

router.onChange(onRoute);
router.start();
store.start();
