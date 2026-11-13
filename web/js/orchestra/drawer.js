// The agent drawer: a panel docked to the right edge of the canvas (not modal, so the canvas stays alive behind it).
import { h, replace } from '../core/dom.js';
import { popIn, popOut } from '../core/motion.js';
import {
  button, iconButton, input, textarea, field, select, segmented, badge, statusDot, runtimeMark, contextDial, confirm, pickFolder, icon, attachAgentHover,
} from '../ui/index.js';
import { initials, fmtTokens, fmtRelative, fmtDuration, runStatusLabel, agentStatusLabel, fmtContext, ctxPercent, fmtPercent, RUNTIME_LABEL, plural } from '../core/format.js';

const PERMS = [
  { value: 'read', label: 'Чтение', note: 'Читает файлы и ищет по проекту, ничего не изменяет' },
  { value: 'edit', label: 'Правка', note: 'Читает и меняет файлы проекта, опасные команды не выполняет' },
  { value: 'full', label: 'Полная автономия', note: 'Выполняет любые команды без запроса подтверждения' },
];
const RUNTIME_ORDER = ['claude_code', 'opencode', 'demo'];

function modelOptions(store, agent) {
  const rt = store.runtimeById(agent.runtime);
  const opts = [{ value: '', label: 'По умолчанию для среды' }];
  for (const m of (rt && rt.models) || []) {
    const where = m.location === 'local' ? 'локально' : 'облако';
    opts.push({ value: m.id, label: m.label || m.id, hint: `${where}${m.context_window ? ' · ' + fmtTokens(m.context_window) : ''}` });
  }
  if (agent.model && !opts.some((o) => o.value === agent.model)) opts.push({ value: agent.model, label: agent.model, hint: 'вручную' });
  return opts;
}

const runDot = (r) => (r.status === 'running' || r.status === 'queued' ? 'working' : r.status === 'error' ? 'error' : r.status === 'ok' ? 'ok' : 'idle');

/**
 * createDrawer({ store, api, router, onClose, onDelete(agent), onSelectAgent(id), onAssign(id), onRemoveEdge(edge), onError(msg) })
 * -> { el, open(agentId, { focusName }), close(), sync(), isOpen, agentId }
 */
export function createDrawer({ store, api, router, onClose, onDelete, onSelectAgent, onAssign, onRemoveEdge }) {
  const el = h('aside.orch-drawer', { role: 'complementary', 'aria-label': 'Свойства агента', hidden: true });
  let agentId = null;
  let ui = null;
  let offRuns = null;
  let hoverOffs = [];
  let savedT = 0;

  const agent = () => store.agentById(agentId);

  function flash(text, tone) {
    if (!ui) return;
    ui.status.textContent = text;
    ui.status.dataset.tone = tone || '';
    clearTimeout(savedT);
    if (text) savedT = setTimeout(() => { if (ui) { ui.status.textContent = 'Изменения сохраняются сразу'; ui.status.dataset.tone = ''; } }, 2200);
  }
  async function save(patch) {
    const a = agent();
    const u = ui;
    if (!a || !u) return false;
    try {
      await api.patch(`/agents/${a.id}`, patch);
      if (ui === u) { flash('Сохранено'); u.err.hidden = true; }
      return true;
    } catch (e) {
      if (ui === u) {
        u.err.textContent = e.message || 'Не удалось сохранить';
        u.err.hidden = false;
        flash('Не сохранено', 'err');
        sync(true);
      }
      return false;
    }
  }

  function build(a) {
    const dial = contextDial({ size: 56, center: 'percent' });
    const title = h('div.orch-drawer__name.truncate');
    const sub = h('div.orch-drawer__sub', h('span.orch-drawer__rt'), statusDot({ status: 'idle' }), h('span.orch-drawer__st'));
    const closeBtn = iconButton({ icon: 'x', label: 'Закрыть панель', size: 'sm', onClick: () => api_.close() });
    const head = h('header.orch-drawer__head', dial, h('div.orch-drawer__id', title, sub), closeBtn);

    const err = h('div.orch-drawer__err', { role: 'alert', hidden: true });

    // activity
    const actText = h('div.orch-now__text');
    const ctxText = h('div.orch-now__ctx.mono');
    const now = h('section.orch-sec', h('h3.orch-sec__title', 'Сейчас'), actText, ctxText);

    // fields
    const name = input({ value: a.name, ariaLabel: 'Имя' });
    const nameField = field({ label: 'Имя', control: name });
    name.input.maxLength = 40;
    const commitName = async () => {
      const v = name.value.trim();
      const cur = agent();
      if (!cur || v === cur.name) { if (!v && cur) name.value = cur.name; return; }
      if (!v) { nameField.update({ error: 'Имя не может быть пустым' }); name.value = cur.name; return; }
      nameField.update({ error: null });
      const ok = await save({ name: v });
      if (!ok && ui) nameField.update({ error: ui.err.textContent });
    };
    name.input.addEventListener('change', commitName);
    name.input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); name.input.blur(); } });

    const available = new Set(store.get('runtimes').filter((r) => r.available).map((r) => r.id));
    const rtIds = [...new Set([...RUNTIME_ORDER, ...store.get('runtimes').map((r) => r.id)])].filter((id) => store.runtimeById(id) || id === a.runtime);
    const rtSeg = segmented({
      options: rtIds.map((id) => ({ value: id, label: RUNTIME_LABEL[id] || id })), value: a.runtime, ariaLabel: 'Среда запуска',
      onChange: (v) => {
        const info = store.runtimeById(v);
        if (!info || !info.available) { rtSeg.value = agent().runtime; return; }
        const cur = agent();
        const models = (info.models || []).map((m) => m.id);
        save({ runtime: v, model: cur.model && models.includes(cur.model) ? cur.model : null });
      },
    });
    const rtNote = h('div.orch-note', { hidden: true });

    const modelSel = select({ options: modelOptions(store, a), value: a.model || '', ariaLabel: 'Модель', mono: true, onChange: (v) => save({ model: v || null }) });
    const modelFree = input({ placeholder: 'Другая модель, например provider/model-id', mono: true, size: 'sm', ariaLabel: 'Своя модель' });
    const applyFree = () => {
      const v = modelFree.value.trim();
      if (!v) return;
      save({ model: v }).then((ok) => { if (ok) modelFree.value = ''; });
    };
    modelFree.input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); applyFree(); } });
    modelFree.input.addEventListener('change', applyFree);
    const modelBox = h('div.orch-model', modelSel, modelFree);
    const modelField = field({ label: 'Модель', control: modelBox, hint: 'Список зависит от среды. Нужной модели нет в списке: впишите её ниже и нажмите Enter' });
    modelBox.id = '';

    const role = textarea({ value: a.role, rows: 2, ariaLabel: 'Роль', placeholder: 'Одна строка: за что отвечает агент' });
    role.addEventListener('change', () => { if (role.value !== agent().role) save({ role: role.value.trim() }); });
    const roleField = field({ label: 'Роль', control: role, hint: 'Её видят руководители, когда решают, кому поручить задачу' });

    const prompt = textarea({ value: a.system_prompt, rows: 5, ariaLabel: 'Системный промпт', placeholder: 'Дополнительные инструкции, если нужны' });
    prompt.addEventListener('change', () => { if (prompt.value !== agent().system_prompt) save({ system_prompt: prompt.value }); });
    const promptField = field({ label: 'Системный промпт', control: prompt });

    const cwdText = h('span.orch-cwd__path.mono.truncate');
    const cwdBtn = button({ label: 'Выбрать', icon: 'folder-open', variant: 'secondary', size: 'sm', onClick: async () => {
      const cur = agent();
      const start = cur.cwd || (store.get('settings') && store.get('settings').default_cwd) || undefined;
      const p = await pickFolder({ start, title: 'Папка проекта' });
      if (p) save({ cwd: p });
    } });
    const cwdReset = button({ label: 'Сбросить', variant: 'quiet', size: 'sm', onClick: () => save({ cwd: null }) });
    const cwdRow = h('div.orch-cwd', cwdText, cwdBtn, cwdReset);
    const cwdField = field({ label: 'Папка проекта', control: cwdRow, hint: 'Пусто: берётся папка по умолчанию из настроек' });
    cwdRow.id = '';

    const permNote = h('div.orch-note.orch-note--perm', { role: 'status' });
    const permSeg = segmented({
      options: PERMS.map((p) => ({ value: p.value, label: p.label })), value: a.permissions, ariaLabel: 'Права',
      onChange: async (v) => {
        const prev = agent().permissions;
        if (v === 'full') {
          const ok = await confirm({
            title: 'Включить полную автономию?',
            message: `Агент «${agent().name}» будет выполнять любые команды без запроса подтверждения: менять и удалять файлы, запускать программы, обращаться к сети. Включайте, только если доверяете задаче и папке проекта.`,
            confirmLabel: 'Включить полную автономию', danger: true,
          });
          if (!ok) { permSeg.value = prev; permNote.textContent = PERMS.find((p) => p.value === prev).note; return; }
        }
        permNote.textContent = PERMS.find((p) => p.value === v).note;
        permNote.dataset.tone = v === 'full' ? 'warn' : '';
        save({ permissions: v });
      },
    });
    const permBlock = h('div.field', h('div.field__label', 'Права'), permSeg, permNote);
    const rtBlock = h('div.field', h('div.field__label', 'Среда запуска'), rtSeg, rtNote);

    const form = h('section.orch-sec', h('h3.orch-sec__title', 'Настройки'), nameField, rtBlock, modelField, roleField, promptField, cwdField, permBlock);

    // links
    const links = h('section.orch-sec');
    // runs
    const runs = h('section.orch-sec');

    const status = h('span.orch-drawer__saved', { role: 'status' }, 'Изменения сохраняются сразу');
    const del = button({ label: 'Удалить агента', icon: 'trash-2', variant: 'danger', size: 'sm', onClick: () => { const cur = agent(); if (cur && onDelete) onDelete(cur); } });
    const foot = h('footer.orch-drawer__foot', status, del);

    const body = h('div.orch-drawer__body', err, now, form, links, runs);
    replace(el, head, body, foot);
    ui = { dial, title, sub, err, actText, ctxText, name, nameField, rtSeg, rtNote, modelSel, modelBox, role, prompt, cwdText, cwdReset, permSeg, permNote, links, runs, status, body, available };
  }

  const focusedIn = (node) => node && node.contains(document.activeElement);

  function renderLinks(a) {
    hoverOffs.forEach((f) => f()); hoverOffs = [];
    const chip = (other, edge, kind) => {
      const nm = h('button.orch-chip__name', { type: 'button', onclick: () => onSelectAgent && onSelectAgent(other.id) }, runtimeMark({ runtime: other.runtime, size: 14 }), h('span.truncate', other.name));
      hoverOffs.push(attachAgentHover(nm, other.id).destroy);
      return h('span.orch-chip', nm, iconButton({ icon: 'x', label: kind === 'sup' ? `Убрать руководителя ${other.name}` : `Убрать подопечного ${other.name}`, size: 'sm', onClick: () => onRemoveEdge && onRemoveEdge(edge) }));
    };
    const edges = store.get('edges');
    const sups = edges.filter((e) => e.subordinate_id === a.id).map((e) => ({ e, o: store.agentById(e.supervisor_id) })).filter((x) => x.o);
    const subs = edges.filter((e) => e.supervisor_id === a.id).map((e) => ({ e, o: store.agentById(e.subordinate_id) })).filter((x) => x.o);
    replace(ui.links,
      h('h3.orch-sec__title', 'Связи'),
      h('div.orch-rel', h('div.orch-rel__label', 'Руководители'), sups.length ? h('div.orch-chips', sups.map((x) => chip(x.o, x.e, 'sup'))) : h('span.orch-none', 'Никому не подчиняется')),
      h('div.orch-rel', h('div.orch-rel__label', 'Подопечные'), subs.length ? h('div.orch-chips', subs.map((x) => chip(x.o, x.e, 'sub'))) : h('span.orch-none', 'Подопечных нет')),
      button({ label: 'Назначить подопечного', icon: 'link-2', variant: 'secondary', size: 'sm', onClick: () => onAssign && onAssign(a.id) }));
  }

  function renderRuns(a) {
    const list = store.recentRuns(400).filter((r) => r.agent_id === a.id).slice(0, 6);
    replace(ui.runs,
      h('h3.orch-sec__title', 'Последние запуски'),
      list.length ? h('ul.orch-runs', list.map((r) => h('li', h('a.orch-run', { href: router.href(`/runs/${r.id}`), title: r.task },
        statusDot({ status: runDot(r), label: runStatusLabel(r.status) }),
        h('span.orch-run__task.truncate', (r.task || '').split('\n')[0] || 'Без описания'),
        h('span.orch-run__meta.mono', r.status === 'running' ? 'идёт' : r.duration_ms != null ? fmtDuration(r.duration_ms) : fmtRelative(r.started_at)))))) : h('span.orch-none', 'Запусков пока не было'));
  }

  function sync(force = false) {
    const a = agent();
    if (!a || !ui) return;
    const lv = a.live || {};
    ui.title.textContent = a.name;
    ui.sub.firstChild.replaceChildren(runtimeMark({ runtime: a.runtime, size: 14 }));
    ui.sub.children[1].dataset.status = lv.status || 'idle';
    ui.sub.lastChild.textContent = agentStatusLabel(lv.status || 'idle');
    if (a.demo && !ui.sub.querySelector('.badge')) ui.sub.append(badge({ text: 'демо' }));
    ui.dial.update({ used: lv.context_used ?? null, window: lv.context_window ?? null, initials: initials(a.name) });
    const busy = lv.status === 'working' || lv.status === 'delegating';
    ui.actText.textContent = busy && lv.activity ? lv.activity : lv.status === 'error' ? 'Последний запуск завершился ошибкой' : 'Ничего не делает';
    ui.actText.classList.toggle('is-idle', !busy);
    const pct = ctxPercent(lv.context_used, lv.context_window);
    ui.ctxText.textContent = pct == null ? 'Контекст: данных пока нет' : `Контекст ${fmtPercent(pct)}: ${fmtContext(lv.context_used, lv.context_window)}`;

    if (force || !focusedIn(ui.nameField)) ui.name.value = a.name;
    ui.rtSeg.value = a.runtime;
    const buttons = ui.rtSeg.querySelectorAll('.seg__item');
    const missing = [];
    buttons.forEach((b) => {
      const info = store.runtimeById(b.dataset.value);
      const off = !info || !info.available;
      b.disabled = off && b.dataset.value !== a.runtime;
      b.title = off ? (info && info.error) || 'Среда недоступна' : '';
      if (off) missing.push(info);
    });
    const unavailable = store.get('runtimes').filter((r) => !r.available);
    ui.rtNote.hidden = !unavailable.length;
    ui.rtNote.textContent = unavailable.map((r) => `${r.label || r.id} недоступен: ${r.error || 'не найден'}`).join(' ');
    ui.modelSel.setOptions(modelOptions(store, a));
    ui.modelSel.value = a.model || '';
    if (force || document.activeElement !== ui.role) ui.role.value = a.role;
    if (force || document.activeElement !== ui.prompt) ui.prompt.value = a.system_prompt;
    const cwd = a.cwd || (store.get('settings') && store.get('settings').default_cwd) || '';
    ui.cwdText.textContent = cwd || 'не задана';
    ui.cwdText.title = cwd;
    ui.cwdText.classList.toggle('is-default', !a.cwd);
    ui.cwdReset.hidden = !a.cwd;
    ui.permSeg.value = a.permissions;
    ui.permNote.textContent = PERMS.find((p) => p.value === a.permissions).note;
    ui.permNote.dataset.tone = a.permissions === 'full' ? 'warn' : '';
    renderLinks(a);
    renderRuns(a);
  }

  const api_ = {
    el,
    get isOpen() { return !el.hidden; },
    get agentId() { return agentId; },
    open(id, { focusName = false } = {}) {
      const a = store.agentById(id);
      if (!a) return;
      const wasOpen = !el.hidden;
      if (agentId !== id || !ui) {
        agentId = id;
        hoverOffs.forEach((f) => f()); hoverOffs = [];
        build(a);
        if (offRuns) offRuns();
        offRuns = store.subscribeRuns(() => { const c = agent(); if (c && ui) renderRuns(c); });
        store.loadRuns({ agent_id: id, limit: 8 }).catch(() => {});
      }
      el.getAnimations().forEach((x) => x.cancel());
      el.hidden = false;
      sync(true);
      if (!wasOpen) popIn(el, { dir: 'right', distance: 8, duration: 200 });
      if (focusName) { ui.name.focus(); ui.name.input.select(); }
    },
    close() {
      if (el.hidden) return;
      const node = el;
      agentId = null;
      ui = null;
      if (offRuns) { offRuns(); offRuns = null; }
      hoverOffs.forEach((f) => f()); hoverOffs = [];
      popOut(node, { duration: 150 }).then(() => { if (!agentId) { node.getAnimations().forEach((x) => x.cancel()); node.hidden = true; node.replaceChildren(); } });
      if (onClose) onClose();
    },
    sync() {
      if (el.hidden || !agentId) return;
      if (!agent()) { api_.close(); return; }
      sync();
    },
    destroy() { if (offRuns) offRuns(); hoverOffs.forEach((f) => f()); el.remove(); },
  };
  return api_;
}
