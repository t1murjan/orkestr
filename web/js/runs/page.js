// A single run: header, delegation timeline with replay, step list and detail.
import { h } from '../core/dom.js';
import { store } from '../core/store.js';
import { api } from '../core/api.js';
import { router } from '../core/router.js';
import { fmtInt, fmtTokens, fmtCost, fmtDuration, fmtDateTime, fmtPercent, ctxPercent, runStatusLabel, ORIGIN_LABEL, pl, FORMS, NB } from '../core/format.js';
import { card, button, badge, statusDot, runtimeMark, contextDial, attachAgentHover, emptyState, segmented, tabs, skeleton, confirm, toast, icon } from '../ui/index.js';
import { createTimeline } from './timeline.js';
import { createStepList, createDetail } from './steps.js';
import { createReplay } from './replay.js';
import { DOT, isLive, xicon, runAgentName } from './util.js';

export function mountRunPage(root, ctx) {
  const id = ctx.params.id;
  const offs = [];
  let timers = [];
  let destroyed = false;
  root.replaceChildren(h('div.rp-load', card({ padding: 'lg', children: skeleton({ lines: 4 }) })));

  store.loadRun(id).then((d) => { if (!destroyed) build(d.run); }).catch((e) => {
    if (destroyed) return;
    const missing = e && (e.code === 'not_found' || e.status === 404);
    root.replaceChildren(card({ padding: 'lg', children: emptyState({
      title: missing ? 'Такого запуска нет' : 'Не удалось загрузить запуск',
      text: missing ? 'Возможно, его удалили вместе с демо-данными. Вернитесь к списку' : (e && e.message) || 'Проверьте связь с сервером',
      action: { label: 'К списку запусков', onClick: () => router.navigate('/runs') } }) }));
  });

  function build(run0) {
    const rootId = run0.root_run_id || run0.id;
    const S = { selStep: null, selLane: run0.id, follow: true, tab: 'result', filterLane: false, tree: { runs: [], steps: [] } };
    const run = () => store.run(id) || run0;

    // ---- header -------------------------------------------------------------
    const dial = contextDial({ size: 56, center: 'percent' });
    const statusEl = h('span.rh__status');
    const durEl = h('span.rh__fig.mono');
    const ctxUsed = h('span.rh__fig.mono');
    const taskEl = h('div.rh__task');
    const fact = (label, node, cls = '') => h('div', { class: ['rh__fact', cls] }, h('span.rh__label', label), node);
    const tIn = h('span.rh__fig.mono'), tOut = h('span.rh__fig.mono'), tCache = h('span.rh__fig.mono'), cost = h('span.rh__fig.mono'), started = h('span.rh__fig.mono'), calls = h('span.rh__fig.mono'), dels = h('span.rh__fig.mono');
    const nameText = h('span.rh__agent'), badges = h('span.rh__badges'), nameEl = h('span.rh__name', nameText, badges), modelEl = h('span.rh__model.mono.truncate'), marks = h('span.rh__marks');
    const parentLink = h('a.rh__parent', { href: '#' });
    parentLink.hidden = true;
    const header = card({ padding: 'lg', className: 'rh', children: [
      h('div.rh__top', h('div.rh__id', marks, nameEl, statusEl), modelEl),
      parentLink, taskEl,
      h('div.rh__facts',
        h('div.rh__ctx', dial, h('div.rh__ctxtext', h('span.rh__label', 'Контекст'), ctxUsed)),
        fact('Токены на входе', tIn), fact('Токены на выходе', tOut), fact('Из кэша', tCache),
        fact('Стоимость', cost), fact('Длительность', durEl), fact('Начат', started), fact('Вызовы', calls), fact('Делегирования', dels)),
    ] });

    // ---- timeline + replay ----------------------------------------------------
    const detail = createDetail({ onOpenLane: (rid) => selectLane(rid) });
    const list = createStepList({ onSelect: (s) => selectStep(s.id, s.run_id, true) });
    const tl = createTimeline({
      onSelectStep: (sid, rid) => selectStep(sid, rid, true),
      onSelectLane: (rid) => selectLane(rid),
      onSeek: (t) => replay.seek(t),
    });
    const replay = createReplay({ getRange: () => tl.range(), onChange: onPlayhead });
    const tlCard = card({ title: 'Дерево делегирования', subtitle: 'Дорожка на каждый запуск, шаги по времени', padding: 'md', className: 'rt', children: [tl.el, replay.el] });

    // ---- list + detail ---------------------------------------------------------
    const filter = segmented({ options: [{ value: 'all', label: 'Все агенты' }, { value: 'lane', label: 'Выбранный' }], value: 'all', size: 'sm', ariaLabel: 'Какие шаги показывать', onChange: (v) => { S.filterLane = v === 'lane'; list.setFilter(S.filterLane ? S.selLane : null); } });
    const listCard = card({ title: 'Шаги', actions: filter, padding: 'none', className: 'rl', children: list.el });
    const tabBar = tabs({ tabs: [{ id: 'step', label: 'Шаг' }, { id: 'result', label: 'Итог' }], value: 'result', ariaLabel: 'Что показывать', onChange: (v) => { S.tab = v; showDetail(); } });
    const detailCard = card({ title: 'Подробности', actions: tabBar, padding: 'md', className: 'rd', children: detail.el });
    const lower = h('div.run__lower', listCard, detailCard);

    root.replaceChildren(h('div.run', header, tlCard, lower));

    // ---- actions (top bar) -----------------------------------------------------
    const stopBtn = button({ label: 'Остановить', variant: 'danger', size: 'md', icon: 'square', onClick: async () => {
      const ok = await confirm({ title: 'Остановить запуск?', message: 'Запуск и все его делегирования будут прерваны.', confirmLabel: 'Остановить', danger: true });
      if (!ok) return;
      stopBtn.update({ loading: true });
      try { await api.post(`/runs/${encodeURIComponent(id)}/cancel`); toast('Запуск остановлен', { kind: 'ok' }); }
      catch (e) { toast(e.message || 'Не удалось остановить запуск', { kind: 'error' }); }
      stopBtn.update({ loading: false });
    } });
    const back = button({ label: 'Все запуски', variant: 'quiet', icon: 'history', onClick: () => router.navigate('/runs') });
    ctx.setActions(back, stopBtn);

    // ---- behaviour -------------------------------------------------------------
    function stepsList() { return S.tree.steps; }
    function selectLane(rid) {
      S.selLane = rid;
      tl.select({ stepId: S.selStep, runId: rid });
      if (S.filterLane) list.setFilter(rid);
    }
    function selectStep(sid, rid, explicit) {
      if (explicit) S.follow = false;
      S.selStep = sid; if (rid) S.selLane = rid;
      tl.select({ stepId: explicit ? sid : null, runId: S.selLane });
      list.setSelected(explicit ? sid : null);
      if (explicit) { S.tab = 'step'; tabBar.value = 'step'; }
      if (S.filterLane) list.setFilter(S.selLane);
      showDetail();
    }
    function showDetail() {
      const r = run();
      const step = S.selStep && stepsList().find((s) => s.id === S.selStep);
      if (S.tab === 'step' && step) {
        const owner = S.tree.runs.find((x) => x.id === step.run_id);
        const child = step.child_run_id ? S.tree.runs.find((x) => x.id === step.child_run_id) : null;
        detail.showStep(step, owner, child);
      } else if (S.tab === 'step') {
        detail.showResult(r);
      } else detail.showResult(r);
    }

    function currentAt(t) {
      let best = null;
      for (const s of stepsList()) {
        if (s.started_at > t) continue;
        const end = s.ended_at != null ? s.ended_at : s.started_at;
        if (s.kind !== 'delegation' && t <= end) { if (!best || s.started_at >= best.started_at) best = s; }
      }
      if (best) return best;
      for (const s of stepsList()) if (s.started_at <= t && (!best || s.started_at > best.started_at)) best = s;
      return best;
    }
    function onPlayhead(t) {
      tl.setPlayhead(t);
      if (t == null) { list.setHidden(null); list.setCurrent(null); return; }
      const future = new Set(stepsList().filter((s) => s.started_at > t).map((s) => s.id));
      list.setHidden(future);
      const cur = currentAt(t);
      if (cur) {
        list.setCurrent(cur.id, true);
        S.selStep = cur.id; S.selLane = cur.run_id; S.follow = false;
        tl.select({ stepId: cur.id, runId: cur.run_id });
        list.setSelected(cur.id);
        S.tab = 'step'; tabBar.value = 'step';
        showDetail();
      }
    }

    function paintHeader() {
      const r = run();
      marks.replaceChildren(runtimeMark({ runtime: r.runtime, size: 18 }));
      nameText.textContent = runAgentName(r);
      badges.replaceChildren(...[r.demo ? badge({ text: 'демо' }) : null, badge({ text: ORIGIN_LABEL[r.origin] || r.origin })].filter(Boolean));
      statusEl.replaceChildren(statusDot({ status: DOT[r.status] || 'idle' }), runStatusLabel(r.status));
      statusEl.dataset.status = r.status;
      modelEl.textContent = r.model || '';
      taskEl.textContent = r.task || '';
      dial.update({ used: r.context_used, window: r.context_window });
      const pct = ctxPercent(r.context_used, r.context_window);
      ctxUsed.textContent = pct == null ? 'нет данных' : `${fmtInt(r.context_used)}${NB}/${NB}${fmtInt(r.context_window)}`;
      tIn.textContent = fmtTokens(r.tokens_in); tOut.textContent = fmtTokens(r.tokens_out);
      tCache.textContent = fmtTokens((r.cache_read_tokens || 0) + (r.cache_write_tokens || 0));
      cost.textContent = fmtCost(r.cost_usd);
      started.textContent = fmtDateTime(r.started_at);
      calls.textContent = fmtInt(r.tool_calls); dels.textContent = fmtInt(r.delegations);
      tickDuration();
      const live = isLive(r);
      stopBtn.hidden = !(live && r.origin !== 'external');
      if (r.parent_run_id) {
        parentLink.hidden = false;
        parentLink.href = router.href('/runs/' + r.root_run_id);
        parentLink.replaceChildren(icon('corner-left-up', { size: 14 }), 'Корневой запуск');
      }
      ctx.setTitle('Запуск', runAgentName(r));
    }
    function tickDuration() {
      const r = run();
      durEl.textContent = isLive(r) ? fmtDuration(Date.now() - r.started_at) : fmtDuration(r.duration_ms);
    }

    function refresh() {
      if (destroyed) return;
      const t = store.treeOf(rootId);
      S.tree = t;
      const r = run();
      paintHeader();
      tl.setData(t);
      const { start } = tl.range();
      const anyLive = t.runs.some(isLive);
      list.setData({ runs: t.runs, steps: t.steps, origin: start, live: anyLive });
      replay.setAvailable(!anyLive && t.steps.length > 0);
      replay.refresh();
      // follow the newest step while nobody picked one
      if (S.follow && t.steps.length && anyLive) {
        const running = t.steps.filter((s) => s.status === 'running');
        const pick = (running.length ? running : t.steps).reduce((a, b) => (b.started_at > a.started_at || (b.started_at === a.started_at && b.seq > a.seq) ? b : a));
        S.selStep = pick.id; S.tab = 'step'; tabBar.value = 'step';
        list.setCurrent(pick.id, false);
        selectLaneQuiet(pick.run_id);
      } else if (S.follow && !anyLive && S.tab !== 'result') { S.tab = 'result'; tabBar.value = 'result'; }
      showDetail();
    }
    function selectLaneQuiet(rid) { S.selLane = rid; tl.select({ stepId: null, runId: rid }); }

    if (run0.agent_id) attachAgentHover(nameText, run0.agent_id);
    refresh();
    // a finished run opens on its result; a running one follows the live step
    if (!isLive(run())) { S.tab = 'result'; tabBar.value = 'result'; showDetail(); }

    offs.push(store.subscribeTree(rootId, refresh));
    offs.push(store.subscribeRun(id, refresh));
    timers.push(setInterval(tickDuration, 200));
    offs.push(() => { tl.destroy(); replay.destroy(); });
  }

  return () => { destroyed = true; offs.forEach((f) => f()); timers.forEach(clearInterval); };
}
