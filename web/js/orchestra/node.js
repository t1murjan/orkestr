// One agent drawn as a graph node: the context dial is the node body (initials inside), a compact label hangs beneath it.
import { h } from '../core/dom.js';
import { contextDial, runtimeMark, statusDot, badge, attachAgentHover } from '../ui/index.js';
import { initials, agentStatusLabel, runtimeLabel } from '../core/format.js';
import { NODE_W, NODE_H, R, portOf } from './geometry.js';

/**
 * createNode(agent) -> { el, body, port, update(agent), setPos({x, y}), setState({...}), destroy() }
 * el is the wrapper positioned on the canvas, body the round focusable node, port the small jack on the rim.
 */
export function createNode(agent) {
  const dial = contextDial({ size: 88, center: 'initials', initials: initials(agent.name) });
  const name = h('span.onode__name.truncate');
  const markHost = h('span.onode__mark');
  const model = h('span.onode__model.mono.truncate');
  const dot = statusDot({ status: 'idle' });
  const act = h('span.onode__act.truncate');
  const demo = badge({ text: 'демо' });
  demo.classList.add('onode__demo');
  const reason = h('span.onode__reason', { 'aria-hidden': 'true' });

  const body = h('div.onode', { tabindex: 0, role: 'button', 'aria-pressed': 'false' }, dial);
  const label = h('div.onode__label', name, h('div.onode__line', markHost, model), h('div.onode__state', dot, act));
  const pp = portOf({ x: 0, y: 0 });
  const port = h('button.onode__port', { type: 'button', 'aria-label': `Назначить подопечного: ${agent.name}`, title: 'Протяните связь к подопечному', style: { left: `${pp.x - 8}px`, top: `${pp.y - 8}px` }, dataset: { nopan: '' } });
  const el = h('div.onode-wrap', { dataset: { id: agent.id, nopan: '' }, style: { width: NODE_W + 'px', height: NODE_H + 'px', '--r': R + 'px' } }, body, label, demo, reason, port);

  let last = '';
  let lastRuntime = null;
  const hover = attachAgentHover(el, agent.id);

  const api = {
    el, body, port, hover,
    update(a) {
      const lv = a.live || {};
      const sig = [a.name, a.runtime, a.model, a.demo, lv.status, lv.activity, lv.context_used, lv.context_window, lv.model].join('\u0001');
      if (sig === last) return;
      last = sig;
      name.textContent = a.name;
      name.title = a.name;
      if (lastRuntime !== a.runtime) { markHost.replaceChildren(runtimeMark({ runtime: a.runtime, size: 14 })); lastRuntime = a.runtime; }
      const modelId = lv.model || a.model;
      model.textContent = modelId || 'модель по умолчанию';
      model.title = modelId || `${runtimeLabel(a.runtime)}, модель по умолчанию`;
      demo.hidden = !a.demo;
      const status = lv.status || 'idle';
      dot.update({ status });
      el.dataset.status = status;
      const busy = status === 'working' || status === 'delegating';
      act.textContent = busy && lv.activity ? lv.activity : status === 'error' ? 'Последний запуск с ошибкой' : agentStatusLabel(status);
      act.title = act.textContent;
      dial.update({ used: lv.context_used ?? null, window: lv.context_window ?? null, initials: initials(a.name) });
      body.setAttribute('aria-label', `${a.name}, ${agentStatusLabel(status)}`);
    },
    setPos(p) { el.style.transform = `translate(${p.x}px, ${p.y}px)`; },
    /** mode: null | 'target' | 'invalid'; hot / dim: highlight state of the Obsidian-style focus */
    setState({ selected = false, mode = null, reason: why = '', source = false, hot = false, dim = false } = {}) {
      el.classList.toggle('is-selected', selected);
      el.classList.toggle('is-target', mode === 'target');
      el.classList.toggle('is-invalid', mode === 'invalid');
      el.classList.toggle('is-source', source);
      el.classList.toggle('is-hot', hot);
      el.classList.toggle('is-dim', dim);
      body.setAttribute('aria-pressed', selected ? 'true' : 'false');
      reason.textContent = mode === 'invalid' ? why : '';
    },
    destroy() { hover.destroy(); el.remove(); },
  };
  api.update(agent);
  return api;
}
