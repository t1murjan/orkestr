// Обзор: stat tiles, one main chart (tokens over time), ranked lists, runs over time, what runs now.
import { h } from '../core/dom.js';
import { api } from '../core/api.js';
import { store } from '../core/store.js';
import { router } from '../core/router.js';
import { fmtInt, fmtTokens, fmtCost, fmtDuration, fmtTime, fmtDateTime, pl, FORMS } from '../core/format.js';
import { statTile, card, table, segmented, emptyState, runtimeMark, contextDial, badge, statusDot, attachAgentHover } from '../ui/index.js';
import { lineChart, stackedBars, rankedList } from '../charts/index.js';
import { runAgentName } from '../runs/util.js';

const WINDOWS = [
  { value: '900', label: '15 мин', buckets: 30, vs: 'к предыдущим 15 минутам' },
  { value: '3600', label: '1 ч', buckets: 60, vs: 'к предыдущему часу' },
  { value: '21600', label: '6 ч', buckets: 72, vs: 'к предыдущим 6 часам' },
  { value: '86400', label: '24 ч', buckets: 96, vs: 'к предыдущим 24 часам' },
];
const WINDOW_TEXT = { 900: 'за последние 15 минут', 3600: 'за последний час', 21600: 'за последние 6 часов', 86400: 'за последние 24 часа' };
const RUNS_FORMS = ['запуск', 'запуска', 'запусков'];

let offs = [];
let timers = [];

/** change vs the previous window; a zero baseline shows the absolute change instead of a percent */
function change(cur, prev, vs, goodWhen) {
  if (cur == null || prev == null) return { text: '—', vs };
  if (prev === 0) return cur === 0 ? { delta: 0, text: '0', vs } : { text: '—', vs: 'до этого запусков не было' };
  return { cur, prev, vs, goodWhen };
}

export default {
  id: 'overview',
  title: 'Обзор',
  layout: 'page',
  mount(root, ctx) {
    let win = WINDOWS.find((w) => w.value === (ctx.query && ctx.query.w)) || WINDOWS[1];
    let alive = true, data = null, loadingNow = false;

    const switcher = segmented({ options: WINDOWS.map((w) => ({ value: w.value, label: w.label })), value: win.value, ariaLabel: 'Окно времени', onChange: (v) => {
      win = WINDOWS.find((w) => w.value === v);
      router.setQuery({ w: v === '3600' ? null : v }, { replace: true });
      subtitle.textContent = WINDOW_TEXT[v];
      load();
    } });
    ctx.setActions(switcher);

    const tiles = {
      active: statTile({ label: 'Активные запуски', value: null }),
      runs: statTile({ label: 'Запуски', value: null }),
      errors: statTile({ label: 'Ошибки', value: null }),
      tokens: statTile({ label: 'Токены', value: null, format: fmtTokens }),
      cost: statTile({ label: 'Стоимость', value: null, format: fmtCost }),
      avg: statTile({ label: 'Среднее время', value: null, format: fmtDuration }),
    };
    const tileRow = h('div.ov__tiles', Object.values(tiles).map((t) => h('div.ov__tile', t)));

    const subtitle = h('span', WINDOW_TEXT[win.value]);
    const tokChart = lineChart({ x: [], series: [{ id: 'in', label: 'На входе', values: [] }, { id: 'out', label: 'На выходе', values: [] }], height: 300, accent: 'in', format: fmtTokens, formatAxis: (n) => fmtTokens(n), ariaLabel: 'Токены на входе и выходе по времени' });
    const mainCard = card({ title: 'Токены по времени', subtitle, padding: 'lg', className: 'ov__main', children: tokChart });

    const agentList = rankedList({ items: [], format: fmtTokens, ariaLabel: 'Агенты по токенам' });
    const agentEmpty = h('p.ov__none', 'За это окно запусков не было');
    const agentCard = card({ title: 'Агенты', subtitle: 'По токенам', padding: 'lg', className: 'ov__agents', children: [agentList, agentEmpty] });

    const runsChart = stackedBars({ x: [], series: [{ id: 'ok', label: 'Успешно', values: [] }, { id: 'err', label: 'Ошибки', values: [], color: 'var(--err)' }], height: 180, accent: 'none', format: fmtInt, formatAxis: fmtInt, ariaLabel: 'Запуски по времени' });
    const runsCard = card({ title: 'Запуски по времени', subtitle: 'Успешные и с ошибкой', padding: 'lg', children: runsChart });
    const modelList = rankedList({ items: [], format: fmtTokens, accent: 'none', ariaLabel: 'Модели по токенам' });
    const modelEmpty = h('p.ov__none', 'Нет данных');
    const rtList = rankedList({ items: [], format: fmtTokens, accent: 'none', ariaLabel: 'Среды выполнения по токенам' });
    const rtEmpty = h('p.ov__none', 'Нет данных');
    const modelCard = card({ title: 'Модели', subtitle: 'По токенам', padding: 'lg', children: [modelList, modelEmpty] });
    const rtCard = card({ title: 'Среды выполнения', subtitle: 'По токенам', padding: 'lg', children: [rtList, rtEmpty] });

    // ---- running now ---------------------------------------------------------
    const dials = new Map();
    const dialFor = (r) => { let d = dials.get(r.id); if (!d) { d = contextDial({ size: 20 }); dials.set(r.id, d); } d.update({ used: r.context_used, window: r.context_window }); return d; };
    const nowTable = table({
      ariaLabel: 'Сейчас выполняется', dense: true, rowKey: (r) => r.id, rows: [],
      columns: [
        { key: 'agent', label: 'Агент', width: 200, render: (r) => { const n = h('span.truncate', runAgentName(r)); if (r.agent_id) attachAgentHover(n, r.agent_id); return h('span.row.gap-2', runtimeMark({ runtime: r.runtime }), n, r.demo ? badge({ text: 'демо' }) : null); } },
        { key: 'task', label: 'Задача', width: '34%', render: (r) => h('span.ov__task.truncate', { title: r.task }, String(r.task || '').replace(/\s+/g, ' ')) },
        { key: 'activity', label: 'Сейчас', render: (r) => h('span.ov__act.truncate.text-2', r.activity || '—') },
        { key: 'dur', label: 'Идёт', align: 'right', width: 100, mono: true, render: (r) => h('span.mono.ov__dur', { dataset: { start: r.started_at } }, fmtDuration(Date.now() - r.started_at)) },
        { key: 'ctx', label: 'Контекст', align: 'right', width: 90, render: (r) => h('span.ov__dial', dialFor(r)) },
      ],
      empty: emptyState({ compact: true, title: 'Сейчас ничего не выполняется', text: 'Дайте задачу агенту на экране «Оркестр»' }),
      onRowClick: (r) => router.navigate('/runs/' + r.root_run_id),
    });
    const nowCount = h('span.muted.mono');
    const nowCard = card({ title: 'Выполняется сейчас', actions: nowCount, padding: 'none', className: 'ov__now', children: nowTable });

    root.replaceChildren(h('div.ov',
      tileRow,
      h('div.ov__row.ov__row--main', mainCard, agentCard),
      h('div.ov__row.ov__row--sub', h('div.ov__col', runsCard, nowCard), h('div.ov__col', modelCard, rtCard))));

    // ---- data ---------------------------------------------------------------------
    function apply(o) {
      const k = o.kpis, p = k.prev || {}, vs = win.vs;
      const tok = (k.tokens_in || 0) + (k.tokens_out || 0), ptok = (p.tokens_in || 0) + (p.tokens_out || 0);
      tiles.active.update({ value: k.active_runs, change: { text: 'сейчас', vs: '' } });
      tiles.runs.update({ value: k.runs, change: change(k.runs, p.runs, vs) });
      tiles.errors.update({ value: k.runs_error, change: change(k.runs_error, p.runs_error, vs, 'down') });
      tiles.tokens.update({ value: tok, change: change(tok, ptok, vs) });
      tiles.cost.update({ value: k.cost_usd, change: change(k.cost_usd, p.cost_usd, vs, 'down') });
      tiles.avg.update({ value: k.avg_run_ms, change: change(k.avg_run_ms, p.avg_run_ms, vs, 'down') });
      const s = o.series;
      tokChart.update({ x: s.t, series: [{ id: 'in', label: 'На входе', values: s.tokens_in }, { id: 'out', label: 'На выходе', values: s.tokens_out }] });
      runsChart.update({ x: s.t, series: [
        { id: 'ok', label: 'Успешно', values: s.runs.map((n, i) => Math.max(0, n - (s.errors[i] || 0))) },
        { id: 'err', label: 'Ошибки', values: s.errors, color: 'var(--err)' }] });
      const tk = (r) => (r.tokens_in || 0) + (r.tokens_out || 0);
      const agents = (o.by_agent || []).slice().sort((a, b) => tk(b) - tk(a)).slice(0, 8)
        .map((a) => ({ id: a.agent_id || a.agent_name, label: a.agent_name, value: tk(a), detail: pl(a.runs, RUNS_FORMS), mark: runtimeMark({ runtime: a.runtime, size: 14 }) }));
      agentList.update({ items: agents }); agentList.hidden = !agents.length; agentEmpty.hidden = agents.length > 0;
      const models = (o.by_model || []).slice().sort((a, b) => tk(b) - tk(a)).slice(0, 6)
        .map((m) => ({ id: m.model || '—', label: m.model || 'по умолчанию', value: tk(m), detail: pl(m.runs, RUNS_FORMS), mark: runtimeMark({ runtime: m.runtime, size: 14 }) }));
      modelList.update({ items: models }); modelList.hidden = !models.length; modelEmpty.hidden = models.length > 0;
      const rts = (o.by_runtime || []).slice().sort((a, b) => tk(b) - tk(a))
        .map((r) => ({ id: r.runtime, label: ({ claude_code: 'Claude Code', opencode: 'OpenCode', demo: 'Демо' })[r.runtime] || r.runtime, value: tk(r), detail: pl(r.runs, RUNS_FORMS), mark: runtimeMark({ runtime: r.runtime, size: 14 }) }));
      rtList.update({ items: rts }); rtList.hidden = !rts.length; rtEmpty.hidden = rts.length > 0;
    }
    async function load() {
      if (loadingNow) return;
      loadingNow = true;
      try {
        const o = await api.get('/overview', { window_s: Number(win.value), buckets: win.buckets });
        if (alive) { data = o; apply(o); }
      } catch (e) { /* keep the last picture; the sidebar already shows the connection */ }
      loadingNow = false;
    }
    function paintNow() {
      const rows = store.recentRuns(400).filter((r) => r.status === 'running').sort((a, b) => a.started_at - b.started_at);
      nowTable.update({ rows });
      nowCount.textContent = rows.length ? fmtInt(rows.length) : '';
      for (const k of dials.keys()) if (!rows.some((r) => r.id === k)) dials.delete(k);
    }

    load();
    store.loadRuns({ status: 'running', limit: 100 }).then(paintNow).catch(() => {});
    paintNow();
    let pend = 0;
    offs.push(store.subscribeRuns(() => { clearTimeout(pend); pend = setTimeout(paintNow, 300); }));
    offs.push(() => clearTimeout(pend));
    let kick = 0;
    offs.push(ctx.live.on('run', () => { if (!kick) kick = setTimeout(() => { kick = 0; load(); }, 2500); }));
    offs.push(() => clearTimeout(kick));
    offs.push(ctx.live.onResync(() => load()));
    timers.push(setInterval(load, 10000));
    timers.push(setInterval(() => {
      for (const el of root.querySelectorAll('.ov__dur')) el.textContent = fmtDuration(Date.now() - Number(el.dataset.start));
    }, 1000));
    offs.push(() => { alive = false; });
  },
  unmount() { offs.forEach((f) => f()); offs = []; timers.forEach(clearInterval); timers = []; },
};
