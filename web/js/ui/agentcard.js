// The agent hover card: what an agent is, how full its context is and what it is busy with.
import { h } from '../core/dom.js';
import { store } from '../core/store.js';
import { contextDial } from './dial.js';
import { runtimeMark, statusDot, badge } from './badge.js';
import { hoverCard } from './overlay.js';
import { tweenNumber } from '../core/motion.js';
import { fmtInt, fmtTokens, fmtCost, fmtPercent, agentStatusLabel, runtimeLabel, ctxPercent, NB } from '../core/format.js';

const DASH = '—';

/**
 * agentHoverCard(agent) -> node with update(agent) and destroy().
 * Shows name, runtime mark, model (mono), the dial with used / window and percent, what the agent does now,
 * status, tokens in / out, cost and run count.
 */
export function agentHoverCard(agent) {
  const mark = h('span.ac__mark');
  const name = h('span.ac__name.truncate');
  const statusText = h('span.ac__status-text');
  const dot = statusDot({ status: 'idle' });
  const demo = badge({ text: 'демо' });
  const model = h('div.ac__model.mono.truncate');
  const dial = contextDial({ size: 56, center: 'percent' });
  const used = h('span.ac__used');
  const win = h('span.ac__win');
  const note = h('span.ac__ctx-note');
  const activity = h('div.ac__activity-text');
  const nIn = h('span.ac__n'), nOut = h('span.ac__n'), nCost = h('span.ac__n'), nRuns = h('span.ac__n');
  const stat = (label, valueEl) => h('div.ac__stat', h('span.ac__stat-label', label), valueEl);

  const el = h('div.ac',
    h('div.ac__head',
      h('div.ac__id', mark, name, demo),
      h('div.ac__status', dot, statusText)),
    model,
    h('div.ac__ctx', dial, h('div.ac__ctx-text', h('span.ac__ctx-label', 'Контекст'), h('span.ac__fig', used, win), note)),
    h('div.ac__activity', h('span.ac__stat-label', 'Сейчас'), activity),
    h('div.ac__stats', stat('Токены на входе', nIn), stat('Токены на выходе', nOut), stat('Стоимость', nCost), stat('Запусков', nRuns)));

  let first = true;
  el.update = (a) => {
    if (!a) return;
    const lv = a.live || {};
    mark.replaceChildren(runtimeMark({ runtime: a.runtime, size: 16 }));
    name.textContent = a.name;
    demo.hidden = !a.demo;
    const status = lv.status || 'idle';
    dot.update({ status });
    statusText.textContent = agentStatusLabel(status);
    el.dataset.status = status;
    const modelId = lv.model || a.model;
    model.textContent = modelId || `${runtimeLabel(a.runtime)}, модель по умолчанию`;
    model.title = modelId || '';
    dial.update({ used: lv.context_used ?? null, window: lv.context_window ?? null });
    const pct = ctxPercent(lv.context_used, lv.context_window);
    const known = pct != null;
    tweenNumber(used, known ? lv.context_used : null, { format: (n) => (n == null ? DASH : fmtInt(n)), duration: 300 });
    win.textContent = known ? `${NB}/${NB}${fmtInt(lv.context_window)}` : '';
    note.textContent = known ? `Занято ${fmtPercent(pct)}, осталось ${fmtTokens(Math.max(0, lv.context_window - lv.context_used))}` : 'Данных о контексте пока нет';
    activity.textContent = lv.activity || (status === 'idle' ? 'Ничего не делает' : status === 'error' ? 'Последний запуск завершился ошибкой' : DASH);
    activity.classList.toggle('is-idle', !lv.activity);
    tweenNumber(nIn, lv.tokens_in ?? 0, { format: fmtTokens });
    tweenNumber(nOut, lv.tokens_out ?? 0, { format: fmtTokens });
    tweenNumber(nCost, lv.cost_usd ?? 0, { format: fmtCost });
    tweenNumber(nRuns, lv.runs_total ?? 0, { format: fmtInt });
    first = false;
  };
  el.update(agent);
  return el;
}

/**
 * Show the agent hover card when the pointer rests on `anchor` (also on keyboard focus).
 * ref: an agent id (stays live from the store), an agent object, or a function returning an agent.
 * attachAgentHover(el, agent.id) -> { destroy() }
 */
export function attachAgentHover(anchor, ref, opts = {}) {
  const resolve = () => (typeof ref === 'function' ? ref() : typeof ref === 'string' ? store.agentById(ref) : ref);
  return hoverCard(anchor, () => {
    const a = resolve();
    if (!a) return null;
    const node = agentHoverCard(a);
    let off = null;
    if (typeof ref === 'string') off = store.subscribe('agents', () => { const n = resolve(); if (n) node.update(n); });
    else if (typeof ref === 'function') off = store.subscribe('agents', () => { const n = resolve(); if (n) node.update(n); });
    node.destroy = () => { if (off) off(); };
    return node;
  }, { placement: 'right-start', width: 320, ...opts });
}
