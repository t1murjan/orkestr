// The composer docked at the bottom of the canvas: pick an agent, describe the task, run it (or stop its current run).
import { h } from '../core/dom.js';
import { button, textarea, select, runtimeMark, statusDot, pickFolder, iconButton, toast, tooltip } from '../ui/index.js';
import { agentStatusLabel } from '../core/format.js';

const ERRORS = {
  runtime_unavailable: 'Среда запуска недоступна',
  bad_cwd: 'Папка проекта недоступна',
  empty_task: 'Опишите задачу',
  unknown_agent: 'Агент не найден',
  depth_exceeded: 'Достигнута максимальная глубина делегирования',
  network: 'Нет связи с сервером',
  timeout: 'Сервер не ответил вовремя',
};

/**
 * createComposer({ store, api, router, getSelectedId(), onPick(id) })
 * -> { el, sync(), setAgent(id), focus() }
 */
export function createComposer({ store, api, router, getSelectedId, onPick }) {
  let agentId = null;
  let cwdOverride = null;
  let busy = false;

  const picker = select({ options: [], placeholder: 'Выберите агента', ariaLabel: 'Агент для задачи', size: 'sm', onChange: (v) => { agentId = v; cwdOverride = null; if (onPick) onPick(v); sync(); } });
  picker.classList.add('orch-comp__pick');
  const liveDot = statusDot({ status: 'idle' });
  const liveText = h('span.orch-comp__live.truncate');
  const cwdPath = h('span.orch-comp__cwd.mono.truncate');
  const cwdBtn = iconButton({ icon: 'folder-open', label: 'Выбрать папку для этого запуска', size: 'sm', onClick: async () => {
    const p = await pickFolder({ start: currentCwd() || undefined, title: 'Папка для запуска' });
    if (p) { cwdOverride = p; sync(); }
  } });
  const cwdReset = button({ label: 'Сбросить', variant: 'quiet', size: 'sm', onClick: () => { cwdOverride = null; sync(); } });
  const task = textarea({ rows: 1, ariaLabel: 'Задача', placeholder: 'Опишите задачу выбранному агенту' });
  task.classList.add('orch-comp__task');
  const err = h('div.orch-comp__err', { role: 'alert', hidden: true });
  const run = button({ label: 'Запустить', icon: 'play', variant: 'primary', onClick: () => submit() });
  const stop = button({ label: 'Остановить', icon: 'square', variant: 'secondary', onClick: () => cancel() });
  stop.hidden = true;
  tooltip(run, 'Ctrl+Enter или ⌘+Enter', { placement: 'top' });

  const el = h('section.orch-comp', { 'aria-label': 'Поручить задачу', dataset: { nopan: '' } },
    h('div.orch-comp__meta', picker, liveDot, liveText, h('span.grow'), cwdPath, cwdBtn, cwdReset, stop, run),
    task, err);

  const agent = () => store.agentById(agentId);
  const currentCwd = () => cwdOverride || (agent() && agent().cwd) || (store.get('settings') && store.get('settings').default_cwd) || '';

  function showError(msg) { err.textContent = msg || ''; err.hidden = !msg; }

  async function submit() {
    const a = agent();
    if (!a) { showError('Выберите агента'); picker.focus(); return; }
    const text = task.value.trim();
    if (!text) { showError(ERRORS.empty_task); task.focus(); return; }
    if (busy) return;
    busy = true; showError('');
    run.update({ loading: true });
    try {
      const body = { agent_id: a.id, task: text };
      if (cwdOverride) body.cwd = cwdOverride;
      const r = await api.post('/runs', body);
      task.value = ''; grow();
      toast(`Запущено: ${a.name}`, { kind: 'ok', action: { label: 'Открыть запуск', onClick: () => router.navigate(`/runs/${r.id}`) } });
    } catch (e) {
      const base = ERRORS[e.code];
      showError(base && e.message && !e.message.includes(base) ? `${base}. ${e.message}` : (e.message || base || 'Не удалось запустить'));
    } finally { busy = false; run.update({ loading: false }); sync(); }
  }

  async function cancel() {
    const a = agent();
    const id = a && a.live && a.live.current_run_id;
    if (!id) return;
    stop.update({ loading: true });
    try { await api.post(`/runs/${id}/cancel`); toast('Запуск остановлен', { kind: 'info' }); } catch (e) { showError(e.message || 'Не удалось остановить'); } finally { stop.update({ loading: false }); }
  }

  task.addEventListener('keydown', (e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); submit(); } });
  const grow = () => { task.style.height = 'auto'; task.style.height = Math.min(160, Math.max(44, task.scrollHeight + 2)) + 'px'; };
  task.addEventListener('input', () => { showError(''); grow(); });

  function sync() {
    const agents = store.get('agents');
    const sel = getSelectedId();
    if (sel && sel !== agentId && store.agentById(sel)) { agentId = sel; cwdOverride = null; }
    if (agentId && !store.agentById(agentId)) agentId = null;
    if (!agentId && agents.length === 1) agentId = agents[0].id;
    picker.setOptions(agents.map((a) => ({ value: a.id, label: a.name, mark: runtimeMark({ runtime: a.runtime, size: 14 }) })));
    picker.value = agentId;
    const a = agent();
    const lv = (a && a.live) || {};
    const working = lv.status === 'working' || lv.status === 'delegating';
    liveDot.hidden = !a;
    liveText.hidden = !a;
    if (a) {
      liveDot.update({ status: lv.status || 'idle' });
      liveText.textContent = working && lv.activity ? lv.activity : agentStatusLabel(lv.status || 'idle');
    }
    stop.hidden = !(a && lv.current_run_id && working);
    const cwd = currentCwd();
    cwdPath.textContent = cwd || 'папка не задана';
    cwdPath.title = cwd;
    cwdPath.classList.toggle('is-override', !!cwdOverride);
    cwdReset.hidden = !cwdOverride;
    task.setAttribute('aria-label', a ? `Задача для ${a.name}` : 'Задача');
  }

  return { el, sync, focus: () => task.focus(), setAgent(id) { agentId = id; cwdOverride = null; sync(); } };
}
