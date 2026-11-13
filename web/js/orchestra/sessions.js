// "Сессии в терминале": Claude Code sessions the user started outside the panel, running right now.
import { h } from '../core/dom.js';
import { contextDial, runtimeMark, icon } from '../ui/index.js';
import { fmtTokens, plural } from '../core/format.js';

const SUBAGENT = ['субагент', 'субагента', 'субагентов'];
const KEY = 'orkestr.orchestra.lane';

function remembered() { try { return localStorage.getItem(KEY) === 'collapsed'; } catch (e) { return false; } }
function remember(collapsed) { try { localStorage.setItem(KEY, collapsed ? 'collapsed' : 'open'); } catch (e) { /* storage may be blocked */ } }

function card(router, run) {
  const dial = contextDial({ size: 32 });
  const title = h('span.orch-ses__title.truncate');
  const model = h('span.orch-ses__model.mono.truncate');
  const act = h('span.orch-ses__act.truncate');
  const subs = h('span.orch-ses__subs');
  const el = h('a.orch-ses', { href: router.href(`/runs/${run.id}`) }, dial,
    h('span.orch-ses__text', title, h('span.orch-ses__line', runtimeMark({ runtime: 'claude_code', size: 12 }), model, subs), act));
  return {
    el,
    update(r, subCount) {
      const first = (r.task || '').split('\n')[0].trim();
      title.textContent = first || 'Сессия без названия';
      el.title = first;
      model.textContent = r.model || '';
      act.textContent = r.activity || 'Работает';
      subs.textContent = subCount ? `${subCount} ${plural(subCount, SUBAGENT)}` : '';
      dial.update({ used: r.context_used ?? null, window: r.context_window ?? null });
      el.setAttribute('aria-label', `Сессия в терминале: ${first}. Открыть запуск`);
    },
  };
}

/** createSessionsLane({ store, router }) -> { el, sync() } */
export function createSessionsLane({ store, router }) {
  const cards = new Map();
  let collapsed = remembered();
  const count = h('span.orch-lane__count.mono');
  const chev = h('span.orch-lane__chev', icon('chevron-down'));
  const toggle = h('button.orch-lane__toggle', { type: 'button', 'aria-expanded': String(!collapsed), onclick: () => { collapsed = !collapsed; remember(collapsed); apply(); } },
    chev, h('span.orch-lane__label', 'Сессии в терминале'), count);
  const list = h('div.orch-lane__list');
  const el = h('section.orch-lane', { 'aria-label': 'Сессии в терминале', hidden: true }, toggle, list);

  function apply() {
    toggle.setAttribute('aria-expanded', String(!collapsed));
    el.classList.toggle('is-collapsed', collapsed);
    list.hidden = collapsed;
  }
  function sync() {
    const all = store.recentRuns(1000);
    const roots = all.filter((r) => r.origin === 'external' && !r.agent_id && !r.parent_run_id && r.status === 'running')
      .sort((a, b) => (a.started_at || 0) - (b.started_at || 0));
    el.hidden = !roots.length;
    count.textContent = String(roots.length);
    const ids = new Set(roots.map((r) => r.id));
    for (const [id, c] of cards) if (!ids.has(id)) { c.el.remove(); cards.delete(id); }
    for (const r of roots) {
      let c = cards.get(r.id);
      if (!c) { c = card(router, r); cards.set(r.id, c); }
      const n = all.filter((x) => x.root_run_id === r.id && x.id !== r.id).length;
      c.update(r, n);
      list.appendChild(c.el);
    }
  }
  apply();
  return { el, sync };
}
