// Component kit: every component and chart with plausible Russian mock data.
import { h } from '../js/core/dom.js';
import * as ui from '../js/ui/index.js';
import * as charts from '../js/charts/index.js';
import { fmtInt, fmtTokens, fmtCost, fmtDuration, fmtPercent, fmtBytes, fmtRelative, NB } from '../js/core/format.js';

const { button, iconButton, input, textarea, field, select, segmented, switchControl, badge, statusDot, keycap, runtimeMark, brandMark, contextDial,
  popover, hoverCard, tooltip, drawer, modal, confirm, toast, tabs, table, card, statTile, emptyState, skeleton, codeBlock, pickFolder, agentHoverCard, attachAgentHover, icon, ICON_NAMES } = ui;

// ---- mock data ---------------------------------------------------------------
const now = Date.now();
const mk = (id, name, runtime, model, status, used, win, activity, tin, tout, cost, runs, demo = false) => ({
  id, name, runtime, model, role: '', demo,
  live: { status, active_runs: status === 'idle' ? 0 : 1, activity, context_used: used, context_window: win, model, tokens_in: tin, tokens_out: tout, cost_usd: cost, runs_total: runs, last_run_at: now - 60000 },
});
const AGENTS = [
  mk('a1', 'Дирижёр', 'claude_code', 'claude-sonnet-4-5', 'delegating', 48200, 200000, 'Ждёт ответа от «Программист»', 1284300, 96200, 14.82, 18),
  mk('a2', 'Исследователь', 'claude_code', 'claude-opus-4-1', 'working', 151400, 200000, 'Читает src/app.py', 402900, 31200, 9.31, 7),
  mk('a3', 'Программист', 'opencode', 'qwen2.5-coder-32b-instruct', 'working', 29800, 32768, 'Правит web/js/main.js', 822000, 70400, 0, 12),
  mk('a4', 'Ревьюер', 'opencode', 'gpt-oss-20b', 'idle', null, null, null, 12480, 2210, 0.42, 3),
  mk('a5', 'Тестировщик', 'demo', 'demo-1', 'error', 8400, 128000, null, 5200, 900, 0, 2, true),
];
let seed = 7;
const rnd = () => { seed = (seed * 16807) % 2147483647; return seed / 2147483647; };
const N = 60;
const t0 = now - N * 60000;
const X = Array.from({ length: N }, (_, i) => t0 + i * 60000);
const wave = (base, amp, noise) => Array.from({ length: N }, (_, i) => Math.max(0, Math.round(base + amp * Math.sin(i / 7) + amp * 0.6 * Math.sin(i / 2.3) + rnd() * noise)));
const mkSeries = () => [
  { id: 'in', label: 'Токены на входе', values: wave(24000, 12000, 8000) },
  { id: 'out', label: 'Токены на выходе', values: wave(3200, 1500, 1200) },
];

// ---- layout helpers ------------------------------------------------------------
const sections = [];
function section(id, title, desc, ...kids) {
  sections.push({ id, title });
  return h('section.ksec', { id }, h('div.ksec__head', h('h2', title), desc ? h('p', desc) : null), ...kids);
}
const L = (text) => h('div.klabel', text);

// ---- 1. tokens ---------------------------------------------------------------------
const swatchList = [
  ['--bg-sunken', '#0e0f11'], ['--bg', '#131416'], ['--surface', '#18191c'], ['--raised', '#212327'], ['--raised-2', '#2a2c31'],
  ['--text', '#eceef0'], ['--text-2', '#a2a6ad'], ['--text-3', '#838890'],
  ['--accent', '#36d1c4'], ['--ok', '#7bc47f'], ['--warn', '#e0a84a'], ['--err', '#e5484d'],
  ['--rt-claude', '#d9895f'], ['--rt-opencode', '#7aa7e8'], ['--rt-demo', '#8d9199'], ['--hairline-2', 'rgba(236,238,240,.14)'],
];
const tokensSec = section('tokens', 'Цвета и шрифты', 'Графит с лёгким холодным оттенком, один фосфорный акцент. Plex Sans для интерфейса, Plex Mono для цифр, идентификаторов и моделей.',
  h('div.kswatches', swatchList.map(([n, v]) => h('div.kswatch', h('div.kswatch__chip', { style: { background: `var(${n})` } }), h('div.kswatch__name', n), h('div.kswatch__val', v)))),
  h('div.ktype',
    h('div.ktype__row', h('span.ktype__meta', '28 / 32'), h('span', { style: { fontSize: '28px', lineHeight: '32px', fontWeight: 500 } }, 'Запуски за последний час')),
    h('div.ktype__row', h('span.ktype__meta', '20 / 28'), h('span', { style: { fontSize: '20px', lineHeight: '28px', fontWeight: 500 } }, 'Дирижёр делегирует задачу')),
    h('div.ktype__row', h('span.ktype__meta', '16 / 24'), h('span', { style: { fontSize: '16px', lineHeight: '24px', fontWeight: 500 } }, 'Модели и железо')),
    h('div.ktype__row', h('span.ktype__meta', '14 / 20'), h('span', 'Агент читает проект и передаёт часть работы подчинённым, а вы видите каждый шаг.')),
    h('div.ktype__row', h('span.ktype__meta', '13 / 20'), h('span.text-2', { style: { fontSize: '13px' } }, 'Вторичный текст и подписи к полям')),
    h('div.ktype__row', h('span.ktype__meta', '12 / 16'), h('span.muted', { style: { fontSize: '12px' } }, 'Мета-информация: 12 июня, 14:05, v0.1.0')),
    h('div.ktype__row', h('span.ktype__meta', 'mono 13'), h('span.mono', { style: { fontSize: '13px' } }, `claude-sonnet-4-5   12${NB}480   1,2${NB}млн   $0.42   850${NB}мс   12,4${NB}с   3${NB}мин${NB}05${NB}с   64${NB}%   12,4${NB}ГБ`))));

// ---- 2. context dial ------------------------------------------------------------------
const FILLS = [null, 0, 24, 52, 78, 93, 100];
const dialHead = ['', 'неизвестно', '0 %', '24 %', '52 %', '78 %', '93 %', '100 %'].map((t) => h('div.kdials__h', t));
const rows = [];
for (const size of [20, 32, 56, 88]) {
  const cls = size === 20 ? 'kdials__c--s' : size === 32 ? 'kdials__c--m' : '';
  rows.push(h('div.kdials__r', `${size} px`));
  for (const f of FILLS) {
    rows.push(h(`div.kdials__c.${cls || 'kdials__c--x'}`, contextDial({ size, used: f == null ? null : f * 2000, window: f == null ? null : 200000,
      center: size >= 56 ? (size === 56 ? 'percent' : 'percent') : null })));
  }
}
rows.push(h('div.kdials__r', '56, инициалы'));
FILLS.forEach((f, i) => rows.push(h('div.kdials__c', contextDial({ size: 56, used: f == null ? null : f * 2000, window: f == null ? null : 200000, center: 'initials', initials: ['ДИ', 'ИС', 'ПР', 'РЕ', 'ТЕ', 'АН', 'ОК'][i] }))));
const liveDial = contextDial({ size: 88, used: 48200, window: 200000, center: 'percent' });
const liveSeg = segmented({ ariaLabel: 'Заполнение', value: '24', options: [{ value: '24', label: '24 %' }, { value: '64', label: '64 %' }, { value: '78', label: '78 %' }, { value: '93', label: '93 %' }, { value: 'x', label: 'Неизвестно' }],
  onChange: (v) => liveDial.update(v === 'x' ? { used: null, window: null } : { used: Number(v) * 2000, window: 200000 }) });
const dialSec = section('dial', 'Контекстный индикатор', 'Подпись продукта. Тонкое кольцо с делениями через 10 %: серая дуга до 70 %, жёлтая с 70 %, красная с 90 %. Неизвестный контекст показывается пустым треком, а не нулём.',
  h('div.kdials', ...dialHead, ...rows),
  h('div.krow.gap-8', { style: { alignItems: 'center' } }, liveDial, h('div.kcol', L('Дуга и цвет плавно переходят к новому значению'), liveSeg)));

// ---- 3. hover card ------------------------------------------------------------------------
const chips = AGENTS.map((a) => {
  const c = h('span.kchip', runtimeMark({ runtime: a.runtime }), a.name, a.demo ? badge({ text: 'демо' }) : null);
  attachAgentHover(c, a);
  return c;
});
const modelChips = AGENTS.slice(0, 4).map((a) => { const c = h('span.kchip.kchip--mono', a.model); attachAgentHover(c, a); return c; });
const hoverSec = section('hover', 'Карточка агента', 'Появляется при наведении на агента или модель в любом месте продукта и остаётся открытой, пока курсор над ней. Наведите на любой элемент ниже.',
  h('div.kgrid.kgrid--3', { style: { alignItems: 'start' } },
    h('div.kcol', L('Открытая карточка (работает)'), h('div.popover.hovercard', { style: { position: 'static', width: '320px' } }, agentHoverCard(AGENTS[1]))),
    h('div.kcol', L('Делегирует'), h('div.popover.hovercard', { style: { position: 'static', width: '320px' } }, agentHoverCard(AGENTS[0]))),
    h('div.kcol', L('Ожидает, контекст ещё неизвестен'), h('div.popover.hovercard', { style: { position: 'static', width: '320px' } }, agentHoverCard(AGENTS[3])))),
  h('div.kcol', L('Агенты'), h('div.kchips', chips)),
  h('div.kcol', L('Модели'), h('div.kchips', modelChips)));

// ---- 4. controls ----------------------------------------------------------------------------
const selModel = select({ ariaLabel: 'Модель', value: 'claude-sonnet-4-5', options: [
  { value: 'claude-sonnet-4-5', label: 'claude-sonnet-4-5', hint: '200 тыс.' }, { value: 'claude-opus-4-1', label: 'claude-opus-4-1', hint: '200 тыс.' },
  { value: 'qwen2.5-coder-32b-instruct', label: 'qwen2.5-coder-32b-instruct', hint: '32 тыс.' }, { value: 'gpt-oss-20b', label: 'gpt-oss-20b', hint: '131 тыс.' },
  { value: 'old', label: 'claude-3-haiku', disabled: true }] });
const selRt = select({ ariaLabel: 'Среда', value: 'claude_code', options: [
  { value: 'claude_code', label: 'Claude Code', mark: runtimeMark({ runtime: 'claude_code' }) }, { value: 'opencode', label: 'OpenCode', mark: runtimeMark({ runtime: 'opencode' }) },
  { value: 'demo', label: 'Демо', mark: runtimeMark({ runtime: 'demo' }) }] });
const cwdInput = input({ value: '/home/user/orkestr', mono: true, ariaLabel: 'Папка', icon: 'folder' });
const buttons = section('controls', 'Кнопки и поля', 'Основная кнопка светлая, не акцентная. Акцент остаётся для выбранного, живого и фокуса.',
  h('div.kstage',
    h('div.kcol', L('Кнопки'), h('div.krow', button({ label: 'Дать задачу', variant: 'primary', icon: 'play' }), button({ label: 'Создать агента', variant: 'secondary', icon: 'plus' }), button({ label: 'Отмена', variant: 'quiet' }), button({ label: 'Удалить', variant: 'danger', icon: 'trash-2' }))),
    h('div.kcol', L('Размеры и состояния'), h('div.krow', button({ label: 'Малая', size: 'sm', variant: 'secondary' }), button({ label: 'Обычная', variant: 'secondary' }), button({ label: 'Крупная', size: 'lg', variant: 'primary' }), button({ label: 'Недоступна', variant: 'secondary', disabled: true }), button({ label: 'Запуск', variant: 'secondary', loading: true }))),
    h('div.kcol', L('Кнопки-иконки и клавиши'), h('div.krow', iconButton({ icon: 'copy', label: 'Копировать' }), iconButton({ icon: 'refresh-cw', label: 'Обновить', variant: 'secondary' }), iconButton({ icon: 'trash-2', label: 'Удалить', variant: 'danger' }), iconButton({ icon: 'panel-right', label: 'Панель', pressed: true }), keycap('Esc'), keycap('Ctrl'), keycap('K')))),
  h('div.kform',
    field({ label: 'Имя агента', hint: 'От 1 до 40 символов', control: input({ value: 'Дирижёр' }) }),
    field({ label: 'Среда', control: selRt }),
    field({ label: 'Модель', hint: 'Пусто: модель среды по умолчанию', control: selModel }),
    field({ label: 'Папка проекта', control: cwdInput }),
    field({ label: 'Имя агента', error: 'Это имя уже занято', control: input({ value: 'Ревьюер', invalid: true }) }),
    field({ label: 'Поиск', control: input({ placeholder: 'Агент или модель', icon: 'search' }) }),
    h('div.span-2', field({ label: 'Роль', hint: 'Одна строка: за что отвечает агент. Её видят руководители', control: textarea({ rows: 3, value: 'Проверяет изменения программиста и пишет замечания по существу' }) }))),
  h('div.kstage',
    h('div.kcol', L('Сегменты'), segmented({ ariaLabel: 'Окно', value: '1h', options: [{ value: '15m', label: '15 мин' }, { value: '1h', label: '1 час' }, { value: '6h', label: '6 часов' }, { value: '24h', label: 'Сутки' }] })),
    h('div.kcol', L('Права'), segmented({ ariaLabel: 'Права', value: 'edit', size: 'sm', options: [{ value: 'read', label: 'Чтение' }, { value: 'edit', label: 'Правка' }, { value: 'full', label: 'Полный' }] })),
    h('div.kcol', L('Переключатели'), switchControl({ checked: true, label: 'Следить за сессиями в терминале', hint: 'Только чтение' }), switchControl({ checked: false, label: 'Демо-задачи по расписанию' })),
    h('div.kcol', L('Статусы'), h('div.kcol.gap-2', ...[['idle', 'Ожидает'], ['working', 'Работает'], ['delegating', 'Делегирует'], ['error', 'Ошибка'], ['ok', 'Готово']].map(([s, t]) => h('div.row.gap-2', statusDot({ status: s }), h('span.text-2', t))))),
    h('div.kcol', L('Метки и среды'), h('div.krow', badge({ text: 'демо' }), badge({ text: 'ошибка', tone: 'err' }), badge({ text: 'ctx 32k', tone: 'warn' })), h('div.kcol.gap-2', runtimeMark({ runtime: 'claude_code', label: true }), runtimeMark({ runtime: 'opencode', label: true }), runtimeMark({ runtime: 'demo', label: true }))),
    h('div.kcol', L('Вкладки'), tabs({ ariaLabel: 'Раздел', value: 'tree', tabs: [{ id: 'tree', label: 'Дерево' }, { id: 'steps', label: 'Шаги', count: 24 }, { id: 'result', label: 'Результат' }] }))));

// ---- 5. data display -------------------------------------------------------------------------------
const tableRows = [
  { id: 'r1', agent: AGENTS[0], task: 'Добавить экран «Запуски» и проверить его ревью', status: 'running', dur: 95000, tok: 1284300 + 96200, cost: 0.74, spark: [3, 5, 4, 8, 7, 12, 9, 14] },
  { id: 'r2', agent: AGENTS[1], task: 'Сравнить подходы к переподключению SSE', status: 'ok', dur: 185000, tok: 402900, cost: 2.31, spark: [2, 3, 3, 4, 3, 2, 3, 3] },
  { id: 'r3', agent: AGENTS[2], task: 'Исправить гонку в store.js при перезагрузке /state', status: 'error', dur: 12400, tok: 82000, cost: 0, spark: [9, 7, 8, 3, 2, 1, 1, 0] },
  { id: 'r4', agent: AGENTS[3], task: 'Проверить стили таблицы на узком экране', status: 'ok', dur: 850, tok: 12480, cost: 0.42, spark: [1, 1, 2, 2, 2, 3, 2, 2] },
];
const DOT = { running: 'working', ok: 'ok', error: 'error' };
const STAT = { running: 'Выполняется', ok: 'Готово', error: 'Ошибка' };
const tbl = table({
  ariaLabel: 'Запуски', rowKey: (r) => r.id, selectable: true, selected: 'r2', rows: tableRows,
  columns: [
    { key: 'agent', label: 'Агент', render: (r) => { const s = h('span.row.gap-2', runtimeMark({ runtime: r.agent.runtime }), r.agent.name); attachAgentHover(s, r.agent); return s; } },
    { key: 'task', label: 'Задача', render: (r) => h('span.truncate', { style: { display: 'block', maxWidth: '360px' } }, r.task) },
    { key: 'status', label: 'Статус', render: (r) => h('span.row.gap-2', statusDot({ status: DOT[r.status] }), STAT[r.status]) },
    { key: 'spark', label: 'Токены в минуту', render: (r) => charts.sparkline({ values: r.spark, tone: r.status === 'running' ? 'accent' : 'neutral' }) },
    { key: 'dur', label: 'Длительность', align: 'right', mono: true, render: (r) => fmtDuration(r.dur) },
    { key: 'tok', label: 'Токены', align: 'right', mono: true, render: (r) => fmtTokens(r.tok) },
    { key: 'cost', label: 'Стоимость', align: 'right', mono: true, render: (r) => fmtCost(r.cost) },
  ],
});
const statTiles = h('div.kgrid.kgrid--4',
  statTile({ label: 'Запусков за час', value: 42, change: { delta: 16.7, vs: 'к предыдущему часу' } }),
  statTile({ label: 'Токенов', value: 1292000, format: fmtTokens, change: { delta: -8.2, vs: 'к предыдущему часу', goodWhen: 'down' } }),
  statTile({ label: 'Стоимость', value: 11.4, format: fmtCost, change: { delta: -11.6, vs: 'к предыдущему часу', goodWhen: 'down' } }),
  statTile({ label: 'Среднее время запуска', value: 64000, format: fmtDuration, change: { delta: 10.3, vs: 'к предыдущему часу', goodWhen: 'down' } }));
const codeSample = codeBlock({ label: 'Подключить сессию из терминала', code: 'claude mcp add --transport http orkestr http://127.0.0.1:8787/mcp/agent/a1' });
const dataSec = section('data', 'Таблица, карточки, цифры', 'Числа выровнены вправо и набраны табличными цифрами. Выбранная строка помечена акцентной полосой, строка под курсором и строка с фокусом читаются отдельно.',
  statTiles,
  card({ title: 'Последние запуски', subtitle: 'Выбрана строка «Исследователь», стрелки двигают фокус по строкам', padding: 'none', children: tbl }),
  h('div.kgrid.kgrid--2',
    card({ title: 'Карточка', subtitle: 'Поверхность, волосная линия, радиус 12', children: h('p', 'Карточки не отбрасывают тень и не светятся: глубину задают ступени графита и тонкая граница.') }),
    h('div.kcol', L('Блок кода'), codeSample)),
  h('div.kgrid.kgrid--2',
    card({ padding: 'none', children: emptyState({ icon: 'workflow', title: 'Холст пока пуст', text: 'Создайте агента или загрузите демо-команду', action: { label: 'Загрузить демо-команду' } }) }),
    card({ title: 'Загрузка', children: skeleton({ lines: 4 }) })));

// ---- 6. charts ------------------------------------------------------------------------------------------
const lineSeries = mkSeries();
const line = charts.lineChart({ x: X, series: lineSeries, height: 300, ariaLabel: 'Токены по минутам', formatX: (t) => ui.badge ? new Date(t).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' }) : '' });
const barX = X.slice(-30);
const mkBars = () => [
  { id: 'ok', label: 'Готово', values: Array.from({ length: 30 }, () => Math.round(rnd() * 5 + 1)), color: 'var(--ink-2)' },
  { id: 'cancel', label: 'Отменено', values: Array.from({ length: 30 }, () => (rnd() > 0.8 ? 1 : 0)), color: 'var(--ink-3)' },
  { id: 'err', label: 'Ошибки', values: Array.from({ length: 30 }, () => (rnd() > 0.82 ? Math.ceil(rnd() * 2) : 0)), color: 'var(--err)' },
];
const bars = charts.stackedBars({ x: barX, series: mkBars(), height: 220, ariaLabel: 'Запуски по минутам', formatX: (t) => new Date(t).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' }) });
const modelItems = [
  { id: 'm1', label: 'claude-sonnet-4-5', value: 1380000, mark: runtimeMark({ runtime: 'claude_code' }), detail: '18 запусков' },
  { id: 'm2', label: 'qwen2.5-coder-32b-instruct', value: 892000, mark: runtimeMark({ runtime: 'opencode' }), detail: '12 запусков' },
  { id: 'm3', label: 'claude-opus-4-1', value: 434000, mark: runtimeMark({ runtime: 'claude_code' }), detail: '7 запусков' },
  { id: 'm4', label: 'gpt-oss-20b', value: 14700, mark: runtimeMark({ runtime: 'opencode' }), detail: '3 запуска' },
];
const ranked = charts.rankedList({ items: modelItems, format: fmtTokens, ariaLabel: 'Токены по моделям' });
const mCpu = charts.meter({ label: 'Процессор', value: 38, hint: '16 ядер' });
const mRam = charts.meter({ label: 'Память', value: 21.4, max: 32, display: fmtPercent(66.9), hint: `${fmtBytes(21.4 * 2 ** 30)} из ${fmtBytes(32 * 2 ** 30)}` });
const mVram = charts.meter({ label: 'Видеопамять', value: 21.7, max: 24, display: fmtPercent(90.4), hint: `${fmtBytes(21.7 * 2 ** 30)} из ${fmtBytes(24 * 2 ** 30)}` });
const mSwap = charts.meter({ label: 'Подкачка', value: 17, max: 24, display: fmtPercent(70.8), hint: `${fmtBytes(17 * 2 ** 30)} из ${fmtBytes(24 * 2 ** 30)}` });
const refresh = button({ label: 'Обновить данные', icon: 'refresh-cw', variant: 'secondary', size: 'sm', onClick: () => {
  line.update({ series: mkSeries() });
  bars.update({ series: mkBars() });
  ranked.update({ items: modelItems.map((m) => ({ ...m, value: Math.round(m.value * (0.5 + rnd())) })).sort((a, b) => b.value - a.value) });
  mCpu.update({ value: 20 + rnd() * 75 });
} });
const chartSec = section('charts', 'Графики', 'SVG, приглушённая сетка, подписи моно. Одна акцентная серия на график, остальные на нейтральных ступенях. Наведите курсор на график; кнопка ниже показывает плавное обновление.',
  h('div.krow', refresh),
  h('div.kgrid.kgrid--main', { style: { alignItems: 'start' } },
    card({ title: 'Токены по минутам', subtitle: 'За последний час', children: line }),
    card({ title: 'Токены по моделям', subtitle: 'За последний час', children: ranked })),
  h('div.kgrid.kgrid--main', { style: { alignItems: 'start' } },
    card({ title: 'Запуски по минутам', subtitle: 'Готово, отменено, ошибки', children: bars }),
    card({ title: 'Нагрузка', subtitle: 'Серый до 70 %, жёлтый с 70 %, красный с 90 %', children: h('div.kmeters', mCpu, mRam, mSwap, mVram) })));

// ---- 7. overlays ------------------------------------------------------------------------------------------
const popBtn = button({ label: 'Поповер', variant: 'secondary', icon: 'info', onClick: () => {
  popover({ anchor: popBtn, placement: 'bottom-start', content: h('div', { style: { padding: '16px', width: '260px' } }, h('div.fw-medium', 'Окно контекста'), h('p.text-sm', { style: { marginTop: '4px' } }, 'Сколько токенов занято в текущем запуске и сколько осталось до предела модели.')) });
} });
const tipBtn = iconButton({ icon: 'info', label: 'Подсказка', variant: 'secondary' });
tooltip(tipBtn, 'Подсказка появляется с задержкой');
const overlaysSec = section('overlays', 'Окна, панели, уведомления', 'Диалоги удерживают фокус и закрываются по Escape. Панели и подсказки появляются с коротким подъёмом из точки привязки.',
  h('div.krow',
    popBtn, tipBtn,
    button({ label: 'Модальное окно', variant: 'secondary', onClick: () => {
      const name = input({ value: 'Ревьюер' });
      const m = modal({ title: 'Новый агент', description: 'Имя должно быть уникальным, роль видят руководители.',
        content: h('div.stack.gap-4', field({ label: 'Имя', control: name }), field({ label: 'Среда', control: select({ value: 'opencode', options: [{ value: 'claude_code', label: 'Claude Code' }, { value: 'opencode', label: 'OpenCode' }] }) })),
        footer: [button({ label: 'Отмена', variant: 'quiet', onClick: () => m.close() }), button({ label: 'Создать', variant: 'primary', onClick: () => { m.close(); toast('Агент «Ревьюер» создан', { kind: 'ok' }); } })] });
    } }),
    button({ label: 'Панель справа', variant: 'secondary', onClick: () => {
      const dr = drawer({ title: 'Исследователь', width: 460, content: h('div.stack.gap-4', agentHoverCard(AGENTS[1]), codeBlock({ code: 'claude-opus-4-1\n151 400 / 200 000 токенов' })), footer: [button({ label: 'Закрыть', variant: 'secondary', onClick: () => dr.close() })] });
    } }),
    button({ label: 'Подтверждение', variant: 'secondary', onClick: async () => {
      const ok = await confirm({ title: 'Удалить агента «Ревьюер»?', message: 'Его активные запуски будут отменены, связи удалены.', confirmLabel: 'Удалить', danger: true });
      toast(ok ? 'Агент удалён' : 'Удаление отменено', { kind: ok ? 'ok' : 'info' });
    } }),
    button({ label: 'Выбор папки', variant: 'secondary', icon: 'folder-open', onClick: async () => { const p = await pickFolder({ start: '/home/user' }); if (p) toast('Выбрана папка ' + p, { kind: 'info' }); } }),
    button({ label: 'Уведомление', variant: 'secondary', onClick: () => toast('Связь с сервером восстановлена', { kind: 'ok' }) }),
    button({ label: 'Ошибка', variant: 'secondary', onClick: () => toast('Среда выполнения недоступна', { kind: 'error' }) })),
  h('div.kcol', L('Иконки (Lucide, 16 px, штрих 1,5)'), h('div.krow.gap-4.text-2', ICON_NAMES.map((n) => h('span', { title: n }, icon(n))))));

// ---- page ------------------------------------------------------------------------------------------------------
const main = h('div.kit__main',
  h('div.kit__head', h('h1', 'Каталог компонентов'), h('p', 'Всё, из чего собираются экраны Оркестра, с правдоподобными данными. Страница автономна: бэкенд не нужен.')),
  dialSec, hoverSec, buttons, dataSec, chartSec, overlaysSec, tokensSec);
const ORDER = ['dial', 'hover', 'controls', 'data', 'charts', 'overlays', 'tokens'];
sections.sort((a, b) => ORDER.indexOf(a.id) - ORDER.indexOf(b.id));
const nav = h('nav.kit__nav', { 'aria-label': 'Разделы каталога' }, h('div.kit__brand', brandMark(20), h('span.wordmark', 'Оркестр')), sections.map((s) => h('a', { href: '#' + s.id }, s.title)));
document.body.append(h('div.kit', nav, main));
