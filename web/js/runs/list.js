// Run list: root runs, newest first, live; filters in the URL query; paging with `before`.
import { h } from '../core/dom.js';
import { store } from '../core/store.js';
import { router } from '../core/router.js';
import { fmtTokens, fmtCost, fmtDuration, fmtRelative, fmtDateTime, runStatusLabel, ORIGIN_LABEL } from '../core/format.js';
import { card, table, select, button, emptyState, badge, runtimeMark, statusDot, contextDial, attachAgentHover, skeleton } from '../ui/index.js';
import { DOT, isLive, runAgentName } from './util.js';

const PAGE = 40;
const STATUS_OPTS = [['', 'Любой статус'], ['running', 'Выполняется'], ['ok', 'Готово'], ['error', 'Ошибка'], ['cancelled', 'Отменён'], ['queued', 'В очереди']];
const ORIGIN_OPTS = [['', 'Любое происхождение'], ['user', 'Задача'], ['delegation', 'Делегирование'], ['external', 'Терминал']];
const ORIGIN_NAME = { user: 'Задача', delegation: 'Делегирование', external: 'Терминал' };

export function mountRunList(root, ctx) {
  const offs = [];
  const timers = [];
  const q = ctx.query || {};
  const F = { status: q.status || '', agent: q.agent || '', origin: q.origin || '' };
  const S = { rows: [], more: false, loading: false, loaded: false, gen: 0 };
  const dials = new Map();

  const agentOpts = () => [{ value: '', label: 'Любой агент' }, ...store.get('agents').map((a) => ({ value: a.id, label: a.name, mark: runtimeMark({ runtime: a.runtime, size: 14 }) })), { value: 'external', label: 'Сессии из терминала' }];
  const stSel = select({ options: STATUS_OPTS.map(([value, label]) => ({ value, label })), value: F.status, size: 'sm', ariaLabel: 'Статус', onChange: (v) => setFilter('status', v) });
  const agSel = select({ options: agentOpts(), value: F.agent, size: 'sm', ariaLabel: 'Агент', onChange: (v) => setFilter('agent', v) });
  const orSel = select({ options: ORIGIN_OPTS.map(([value, label]) => ({ value, label })), value: F.origin, size: 'sm', ariaLabel: 'Происхождение', onChange: (v) => setFilter('origin', v) });
  const reset = button({ label: 'Сбросить', variant: 'quiet', size: 'sm', onClick: () => { stSel.value = ''; agSel.value = ''; orSel.value = ''; F.status = F.agent = F.origin = ''; sync(); load(true); } });
  const count = h('span.rn__count.muted');
  const bar = h('div.rn__filters', h('div.rn__fgroup', stSel, agSel, orSel, reset), count);

  const queryParams = () => {
    const p = { root_only: true, limit: PAGE };
    if (F.status) p.status = F.status;
    if (F.origin) p.origin = F.origin;
    if (F.agent === 'external') p.origin = 'external'; else if (F.agent) p.agent_id = F.agent;
    return p;
  };
  const matches = (r) => {
    if (r.parent_run_id) return false;
    if (F.status && r.status !== F.status) return false;
    if (F.origin && r.origin !== F.origin) return false;
    if (F.agent === 'external') return r.origin === 'external';
    if (F.agent && r.agent_id !== F.agent) return false;
    return true;
  };
  function sync() {
    router.setQuery({ status: F.status || null, agent: F.agent || null, origin: F.origin || null }, { replace: true });
    reset.hidden = !(F.status || F.agent || F.origin);
  }
  function setFilter(k, v) { F[k] = v; sync(); load(true); }

  // ---- table ----------------------------------------------------------------
  const dialFor = (r) => {
    let d = dials.get(r.id);
    if (!d) { d = contextDial({ size: 20, used: r.context_used, window: r.context_window }); dials.set(r.id, d); }
    else d.update({ used: r.context_used, window: r.context_window });
    return d;
  };
  const dur = (r) => h('span.mono.rn__dur', { dataset: isLive(r) ? { start: r.started_at } : {} }, isLive(r) ? fmtDuration(Date.now() - r.started_at) : fmtDuration(r.duration_ms));
  const tbl = table({
    ariaLabel: 'Запуски', rowKey: (r) => r.id, dense: false,
    columns: [
      { key: 'status', label: 'Статус', width: 128, render: (r) => h('span.row.gap-2.nowrap', statusDot({ status: DOT[r.status] || 'idle' }), runStatusLabel(r.status)) },
      { key: 'agent', label: 'Агент', width: 200, render: (r) => { const n = h('span.truncate', runAgentName(r)); if (r.agent_id) attachAgentHover(n, r.agent_id); return h('span.row.gap-2.rn__agent', runtimeMark({ runtime: r.runtime }), dialFor(r), n, r.demo ? badge({ text: 'демо' }) : null); } },
      { key: 'task', label: 'Задача', render: (r) => h('span.rn__task.truncate', { title: r.task }, firstLine(r.task)) },
      { key: 'origin', label: 'Происхождение', width: 140, render: (r) => h('span.text-2.nowrap', ORIGIN_NAME[r.origin] || r.origin) },
      { key: 'started_at', label: 'Начат', width: 120, render: (r) => h('span.text-2.nowrap', { title: fmtDateTime(r.started_at) }, fmtRelative(r.started_at)) },
      { key: 'duration', label: 'Длительность', align: 'right', width: 112, mono: true, render: dur },
      { key: 'tokens', label: 'Токены', align: 'right', width: 90, mono: true, render: (r) => fmtTokens((r.tokens_in || 0) + (r.tokens_out || 0)) },
      { key: 'cost_usd', label: 'Стоимость', align: 'right', width: 92, mono: true, render: (r) => fmtCost(r.cost_usd) },
      { key: 'delegations', label: 'Подзапуски', align: 'right', width: 100, mono: true, render: (r) => (r.delegations ? String(r.delegations) : '—') },
    ],
    rows: [],
    empty: h('div'),
    onRowClick: (r) => router.navigate('/runs/' + r.id),
  });
  const emptyHost = h('div.rn__empty');
  const moreBtn = button({ label: 'Показать ещё', variant: 'secondary', onClick: () => loadMore() });
  const foot = h('div.rn__foot', moreBtn);
  const main = card({ padding: 'none', className: 'rn__card', children: [tbl, emptyHost, foot] });
  root.replaceChildren(h('div.rn', bar, main));
  reset.hidden = !(F.status || F.agent || F.origin);

  function firstLine(t) { return String(t || '').replace(/\s+/g, ' ').trim(); }
  function paint() {
    const rows = S.rows.slice().sort((a, b) => (b.started_at ?? Infinity) - (a.started_at ?? Infinity));
    tbl.update({ rows });
    count.textContent = S.loaded && rows.length ? `Показано ${rows.length}` : '';
    foot.hidden = !S.more;
    emptyHost.replaceChildren(...(S.loaded && !rows.length ? [emptyState({ compact: true,
      title: F.status || F.agent || F.origin ? 'Ничего не нашлось' : 'Запусков пока нет',
      text: F.status || F.agent || F.origin ? 'Смените или сбросьте фильтры' : 'Дайте задачу агенту на экране «Оркестр»',
      action: F.status || F.agent || F.origin ? { label: 'Сбросить фильтры', variant: 'secondary', onClick: () => reset.click() } : { label: 'Открыть Оркестр', variant: 'secondary', onClick: () => router.navigate('/') } })] : (S.loaded ? [] : [h('div.rn__skel', skeleton({ lines: 5, height: 20 }))])));
    for (const k of dials.keys()) if (!rows.some((r) => r.id === k)) dials.delete(k);
  }

  async function load(reset_) {
    const gen = ++S.gen;
    if (reset_) { S.rows = []; S.loaded = false; S.more = false; paint(); }
    try {
      const list = await store.loadRuns(queryParams());
      if (gen !== S.gen) return;
      S.rows = list; S.more = list.length >= PAGE; S.loaded = true;
    } catch (e) { if (gen === S.gen) { S.loaded = true; S.rows = []; } }
    paint();
  }
  async function loadMore() {
    const last = S.rows.reduce((a, r) => Math.min(a, r.started_at ?? Infinity), Infinity);
    if (!Number.isFinite(last)) return;
    moreBtn.update({ loading: true });
    const gen = S.gen;
    try {
      const list = await store.loadRuns({ ...queryParams(), before: last });
      if (gen !== S.gen) return;
      const have = new Set(S.rows.map((r) => r.id));
      S.rows = [...S.rows, ...list.filter((r) => !have.has(r.id))];
      S.more = list.length >= PAGE;
    } catch (e) { /* keep what we have */ }
    moreBtn.update({ loading: false });
    paint();
  }

  // live: merge changed runs without refetching; coalesce bursts
  let pend = 0;
  const merge = () => {
    pend = 0;
    if (!S.loaded) return;
    const byId = new Map(S.rows.map((r) => [r.id, r]));
    const oldest = S.more ? S.rows.reduce((a, r) => Math.min(a, r.started_at ?? Infinity), Infinity) : -Infinity;
    for (const r of store.recentRuns(400)) {
      if (!matches(r)) { byId.delete(r.id); continue; }
      if (byId.has(r.id) || (r.started_at ?? Infinity) >= oldest) byId.set(r.id, r);
    }
    S.rows = [...byId.values()];
    paint();
  };
  offs.push(store.subscribeRuns(() => { if (!pend) pend = setTimeout(merge, 250); }));
  offs.push(store.subscribe('agents', () => agSel.setOptions(agentOpts())));
  offs.push(() => clearTimeout(pend));
  // tick durations of running rows and relative start times
  timers.push(setInterval(() => {
    for (const el of root.querySelectorAll('.rn__dur[data-start]')) el.textContent = fmtDuration(Date.now() - Number(el.dataset.start));
  }, 500));
  timers.push(setInterval(() => paint(), 30000));
  offs.push(live_resync());
  function live_resync() { return ctx.live.onResync(() => load(true)); }

  sync();
  load(true);
  return () => { offs.forEach((f) => f()); timers.forEach(clearInterval); };
}
