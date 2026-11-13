// Настройки: limits, default folder, watching, connecting a terminal session, demo data, about.
import { h } from '../core/dom.js';
import { api } from '../core/api.js';
import { store } from '../core/store.js';
import { card, button, input, select, switchControl, codeBlock, emptyState, confirm, toast, pickFolder, runtimeMark, badge } from '../ui/index.js';

let offs = [];
let timers = [];

const LIMITS = [
  { key: 'max_depth', label: 'Глубина делегирования', hint: 'Сколько уровней агентов может поручать работу друг другу. 0 отключает делегирование', min: 0, max: 20 },
  { key: 'max_concurrent_runs', label: 'Одновременных запусков', hint: 'Остальные ждут в очереди', min: 1, max: 64 },
  { key: 'delegate_wait_s', label: 'Ожидание делегата', hint: 'Сколько агент ждёт ответа подчинённого, прежде чем проверить позже', min: 1, max: 3600, unit: 'с' },
  { key: 'run_timeout_s', label: 'Лимит времени запуска', hint: 'После этого запуск останавливается', min: 10, max: 86400, unit: 'с' },
];

export default {
  id: 'settings',
  title: 'Настройки',
  layout: 'page',
  mount(root, ctx) {
    const { store: st } = ctx;
    const rows = [];       // { key, apply(value) } rebuilt from store updates

    // one setting row: label + hint on the left, a control and a quiet "Сохранено" on the right
    function row({ label, hint, control, key, status }) {
      const saved = h('span.st__saved', { 'aria-live': 'polite' });
      const el = h('div.st__row', h('div.st__text', h('div.st__label', label), hint ? h('div.st__hint', hint) : null),
        h('div.st__control', saved, control));
      let t = 0;
      const flash = (text, bad) => {
        saved.textContent = text; saved.dataset.bad = bad ? '1' : '';
        saved.classList.add('is-on');
        clearTimeout(t);
        t = setTimeout(() => saved.classList.remove('is-on'), bad ? 4000 : 1800);
      };
      return { el, flash };
    }
    async function save(key, value, flash, revert) {
      try { await api.patch('/settings', { [key]: value }); flash('Сохранено'); return true; }
      catch (e) { flash(e.message || 'Не удалось сохранить', true); if (revert) revert(); return false; }
    }

    const cur = () => st.get('settings') || {};

    // ---- limits ---------------------------------------------------------------
    const limitRows = LIMITS.map((L) => {
      const inp = input({ type: 'number', value: cur()[L.key] ?? '', mono: true, size: 'sm', suffix: L.unit, ariaLabel: L.label, inputmode: 'numeric' });
      inp.input.min = L.min; inp.input.max = L.max; inp.input.step = 1;
      const r = row({ label: L.label, hint: `${L.hint}. Допустимо от ${L.min} до ${L.max}`, control: inp });
      inp.input.addEventListener('change', () => {
        const n = Number(inp.value);
        if (!Number.isInteger(n) || n < L.min || n > L.max) { r.flash(`Нужно целое число от ${L.min} до ${L.max}`, true); inp.value = cur()[L.key] ?? ''; return; }
        if (n === cur()[L.key]) return;
        save(L.key, n, r.flash, () => { inp.value = cur()[L.key] ?? ''; });
      });
      rows.push({ key: L.key, apply: (v) => { if (document.activeElement !== inp.input) inp.value = v ?? ''; } });
      return r.el;
    });
    const limitsCard = card({ title: 'Запуски', subtitle: 'Ограничения, которые движок применяет к каждому запуску', padding: 'lg', className: 'st__card', children: h('div.st__rows', limitRows) });

    // ---- folder ---------------------------------------------------------------------
    const cwdText = h('span.st__path.mono.truncate');
    const cwdBtn = button({ label: 'Выбрать папку', icon: 'folder-open', variant: 'secondary', size: 'sm', onClick: async () => {
      const p = await pickFolder({ start: cur().default_cwd, title: 'Папка по умолчанию' });
      if (p && p !== cur().default_cwd) save('default_cwd', p, cwdRow.flash);
    } });
    const cwdRow = row({ label: 'Папка по умолчанию', hint: 'Агент без своей папки работает здесь', control: h('div.st__cwd', cwdText, cwdBtn) });
    rows.push({ key: 'default_cwd', apply: (v) => { cwdText.textContent = v || '—'; cwdText.title = v || ''; } });

    // ---- watching ---------------------------------------------------------------------
    const mkSwitch = (key, label, hint) => {
      const sw = switchControl({ checked: !!cur()[key], ariaLabel: label, onChange: (v) => save(key, v, r.flash, () => { sw.checked = !!cur()[key]; }) });
      const r = row({ label, hint, control: sw });
      rows.push({ key, apply: (v) => { sw.checked = !!v; } });
      return r.el;
    };
    const watchCard = card({ title: 'Наблюдение', padding: 'lg', className: 'st__card', children: h('div.st__rows',
      cwdRow.el,
      mkSwitch('watch_external', 'Следить за сессиями из терминала', 'Сессии Claude Code, запущенные в терминале, появятся в запусках. Только чтение'),
      mkSwitch('demo_auto', 'Запускать демо-задачи автоматически', 'Демо-агенты сами берутся за придуманные задачи, чтобы панель не пустовала')) });

    // ---- connect a terminal session -----------------------------------------------------
    const connectBody = h('div.st__connect');
    const connectCard = card({ title: 'Подключить сессию из терминала', padding: 'lg', className: 'st__card', children: connectBody });
    let tpl = null, chosen = null;
    const agentSel = select({ options: [], placeholder: 'Выберите агента', ariaLabel: 'Агент, к которому подключается сессия', onChange: (v) => { chosen = v; paintCommands(); } });
    const cmds = h('div.st__cmds');
    function paintConnect() {
      const agents = st.get('agents') || [];
      if (!agents.length) {
        connectBody.replaceChildren(emptyState({ compact: true, title: 'Пока не к кому подключаться', text: 'Создайте агента на экране «Оркестр» или загрузите демо-команду ниже' }));
        return;
      }
      if (!chosen || !agents.some((a) => a.id === chosen)) chosen = agents[0].id;
      agentSel.setOptions(agents.map((a) => ({ value: a.id, label: a.name, mark: runtimeMark({ runtime: a.runtime, size: 14 }) })));
      agentSel.value = chosen;
      connectBody.replaceChildren(
        h('p.st__lead', 'Сессия, запущенная в терминале, получит инструменты агента и сможет поручать работу его подчинённым. Выполните команду в папке проекта; для OpenCode добавьте фрагмент в opencode.json.'),
        h('div.st__pick', h('span.st__label', 'Агент'), agentSel), cmds);
      paintCommands();
    }
    function paintCommands() {
      if (!tpl) { cmds.replaceChildren(h('p.st__hint', 'Загружаем шаблоны команд')); return; }
      const sub = (s) => String(s || '').replaceAll('{agent_id}', chosen || '');
      cmds.replaceChildren(
        h('div.st__cmd', h('div.st__cmd-t', 'Claude Code'), codeBlock({ code: sub(tpl.claude_command_template) })),
        h('div.st__cmd', h('div.st__cmd-t', 'OpenCode'), codeBlock({ code: sub(tpl.opencode_snippet_template) })));
    }
    api.get('/connect').then((d) => { tpl = (d && d.mcp) || null; paintCommands(); })
      .catch(() => { cmds.replaceChildren(h('p.st__hint', 'Не удалось получить команды. Проверьте связь с сервером')); });

    // ---- demo data -----------------------------------------------------------------------------
    const demoState = h('span.st__hint');
    const seedBtn = button({ label: 'Загрузить демо-команду', variant: 'secondary', size: 'sm', onClick: async () => {
      seedBtn.update({ loading: true });
      try { await api.post('/demo/seed'); toast('Демо-команда загружена', { kind: 'ok', timeout: 2500 }); } catch (e) { toast(e.message || 'Не удалось загрузить', { kind: 'error' }); }
      seedBtn.update({ loading: false });
    } });
    const clearBtn = button({ label: 'Удалить демо-данные', variant: 'danger', size: 'sm', onClick: async () => {
      const ok = await confirm({ title: 'Удалить демо-данные?', message: 'Демо-агенты, их связи и все их запуски будут удалены. Ваши агенты и запуски останутся.', confirmLabel: 'Удалить', danger: true });
      if (!ok) return;
      clearBtn.update({ loading: true });
      try { await api.post('/demo/clear'); toast('Демо-данные удалены', { kind: 'ok', timeout: 2500 }); } catch (e) { toast(e.message || 'Не удалось удалить', { kind: 'error' }); }
      clearBtn.update({ loading: false });
    } });
    const demoCard = card({ title: 'Демо-данные', subtitle: 'Встроенная команда агентов для знакомства с панелью', padding: 'lg', className: 'st__card',
      children: h('div.st__demo', h('div.st__demo-btns', seedBtn, clearBtn), demoState) });
    const paintDemo = () => { const m = st.get('meta'); demoState.textContent = m && m.demo_seeded ? 'Демо-команда сейчас загружена' : 'Демо-команда не загружена'; };

    // ---- about ----------------------------------------------------------------------------------
    const ver = h('span.mono');
    const aboutCard = card({ title: 'О программе', padding: 'lg', className: 'st__card', children: h('div.st__about',
      h('div.st__arow', h('span.st__label', 'Оркестр'), ver),
      h('p.st__hint', 'Локальная панель для агентов Claude Code и OpenCode. Работает только на этой машине.'),
      h('p.st__hint', 'Дизайн выполнен по методу unslop Дамира Соколовского (@heydamir), лицензия CC BY 4.0.')) });
    const paintAbout = () => { const m = st.get('meta'); ver.textContent = m && m.version ? 'версия ' + m.version : ''; };

    root.replaceChildren(h('div.st', limitsCard, watchCard, connectCard, demoCard, aboutCard));

    const paintSettings = (s) => { if (!s) return; for (const r of rows) r.apply(s[r.key]); };
    paintSettings(cur());
    paintConnect(); paintDemo(); paintAbout();
    offs.push(st.subscribe('settings', paintSettings));
    offs.push(st.subscribe('agents', () => { if (!document.activeElement || !connectBody.contains(document.activeElement)) paintConnect(); }));
    offs.push(st.subscribe('meta', () => { paintDemo(); paintAbout(); }));
    api.get('/settings').then((s) => paintSettings(s)).catch(() => {});
  },
  unmount() { offs.forEach((f) => f()); offs = []; timers.forEach(clearInterval); timers = []; },
};
