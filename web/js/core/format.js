// One format per kind of figure, across the whole product (docs/DESIGN.md "Type").
//   tokens   12 480 / 1,2 млн      cost  $0.42      duration  850 мс / 12,4 с / 3 мин 05 с
//   percent  64 %                  bytes 12,4 ГБ    clock     14:05:32
export const NB = ' ';
const MINUS = '−';
const DASH = '—';

const isNum = (n) => typeof n === 'number' && Number.isFinite(n);
const pad2 = (n) => String(n).padStart(2, '0');

function groupDigits(intStr) { return intStr.replace(/\B(?=(\d{3})+(?!\d))/g, NB); }

/** 12480 -> "12 480" */
export function fmtInt(n) {
  if (!isNum(n)) return DASH;
  const r = Math.round(Math.abs(n));
  return (n < 0 && r !== 0 ? MINUS : '') + groupDigits(String(r));
}

/** Decimal with comma: fmtDec(12.43, 1) -> "12,4" */
export function fmtDec(n, digits = 1) {
  if (!isNum(n)) return DASH;
  const [i, f] = Math.abs(n).toFixed(digits).split('.');
  const neg = n < 0 && Number(Math.abs(n).toFixed(digits)) !== 0;
  return (neg ? MINUS : '') + groupDigits(i) + (f ? ',' + f : '');
}

function compactMillions(v, unit) {
  const digits = v >= 100 ? 0 : 1;
  let s = fmtDec(v, digits);
  if (s.endsWith(',0')) s = s.slice(0, -2);
  return s + NB + unit;
}

/** Token counts: 12 480, 1,2 млн, 3,4 млрд */
export function fmtTokens(n) {
  if (!isNum(n)) return DASH;
  const a = Math.abs(n);
  if (a < 1e6) return fmtInt(n);
  if (a < 1e9) return (n < 0 ? MINUS : '') + compactMillions(a / 1e6, 'млн');
  return (n < 0 ? MINUS : '') + compactMillions(a / 1e9, 'млрд');
}

/** Cost in dollars: $0.42, <$0.01, $1 284.50 */
export function fmtCost(n) {
  if (!isNum(n)) return DASH;
  if (n > 0 && n < 0.005) return '<$0.01';
  const [i, f] = Math.abs(n).toFixed(2).split('.');
  return (n < 0 ? MINUS : '') + '$' + groupDigits(i) + '.' + f;
}

/** Durations from milliseconds: 850 мс, 12,4 с, 3 мин 05 с, 1 ч 05 мин */
export function fmtDuration(ms) {
  if (!isNum(ms)) return DASH;
  if (ms < 0) ms = 0;
  if (ms < 1000) return Math.round(ms) + NB + 'мс';
  const tenths = Math.round(ms / 100) / 10;
  if (tenths < 60) return fmtDec(tenths, 1) + NB + 'с';
  const totalS = Math.round(ms / 1000);
  if (totalS < 3600) return Math.floor(totalS / 60) + NB + 'мин' + NB + pad2(totalS % 60) + NB + 'с';
  const totalM = Math.floor(totalS / 60);
  return Math.floor(totalM / 60) + NB + 'ч' + NB + pad2(totalM % 60) + NB + 'мин';
}

/** Percent value (0..100): 64 % */
export function fmtPercent(p, digits = 0) {
  if (!isNum(p)) return DASH;
  return (digits ? fmtDec(p, digits) : fmtInt(p)) + NB + '%';
}

const BYTE_UNITS = ['Б', 'КБ', 'МБ', 'ГБ', 'ТБ'];
/** Bytes: 512 Б, 4,2 МБ, 12,4 ГБ (base 1024) */
export function fmtBytes(n) {
  if (!isNum(n)) return DASH;
  let v = Math.abs(n), u = 0;
  while (v >= 1024 && u < BYTE_UNITS.length - 1) { v /= 1024; u++; }
  const s = u === 0 ? String(Math.round(v)) : fmtDec(v, v >= 100 ? 0 : 1);
  return (n < 0 ? MINUS : '') + s + NB + BYTE_UNITS[u];
}

/** Signed change: +12 %, −3 %, 0 %. unit defaults to "%" */
export function fmtSigned(n, { digits = 0, unit = '%' } = {}) {
  if (!isNum(n)) return DASH;
  const body = digits ? fmtDec(Math.abs(n), digits) : fmtInt(Math.abs(n));
  const zero = Number(Math.abs(n).toFixed(digits)) === 0;
  const sign = zero ? '' : n > 0 ? '+' : MINUS;
  return sign + body + (unit ? NB + unit : '');
}

/** Change of cur against prev, in percent. Returns null when it cannot be computed. */
export function pctChange(cur, prev) {
  if (!isNum(cur) || !isNum(prev) || prev === 0) return null;
  return ((cur - prev) / Math.abs(prev)) * 100;
}

const toDate = (ts) => (ts instanceof Date ? ts : new Date(ts));
/** 14:05:32 */
export function fmtClock(ts) {
  if (!isNum(ts) && !(ts instanceof Date)) return DASH;
  const d = toDate(ts);
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
}
/** 14:05 */
export function fmtTime(ts) {
  if (!isNum(ts) && !(ts instanceof Date)) return DASH;
  const d = toDate(ts);
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
}
const MONTHS = ['янв.', 'февр.', 'мар.', 'апр.', 'мая', 'июн.', 'июл.', 'авг.', 'сент.', 'окт.', 'нояб.', 'дек.'];
/** 13 нояб. */
export function fmtDate(ts) {
  if (!isNum(ts) && !(ts instanceof Date)) return DASH;
  const d = toDate(ts);
  return `${d.getDate()}${NB}${MONTHS[d.getMonth()]}`;
}
/** 13 нояб., 14:05 */
export function fmtDateTime(ts) {
  if (!isNum(ts) && !(ts instanceof Date)) return DASH;
  return `${fmtDate(ts)}, ${fmtTime(ts)}`;
}

/** Relative time: только что, 3 мин назад, 2 ч назад, вчера, 5 дн. назад, 13 нояб. */
export function fmtRelative(ts, now = Date.now()) {
  if (!isNum(ts)) return DASH;
  const s = Math.max(0, (now - ts) / 1000);
  if (s < 5) return 'только что';
  if (s < 60) return Math.floor(s) + NB + 'с назад';
  if (s < 3600) return Math.floor(s / 60) + NB + 'мин назад';
  if (s < 86400) return Math.floor(s / 3600) + NB + 'ч назад';
  const days = Math.floor(s / 86400);
  if (days === 1) return 'вчера';
  if (days < 7) return days + NB + 'дн. назад';
  return fmtDate(ts);
}

/** Russian plural form: plural(3, ['запуск','запуска','запусков']) -> "запуска" */
export function plural(n, forms) {
  const a = Math.abs(Math.trunc(n));
  const m10 = a % 10, m100 = a % 100;
  if (m10 === 1 && m100 !== 11) return forms[0];
  if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return forms[1];
  return forms[2];
}
/** pl(3, FORMS.run) -> "3 запуска" */
export const pl = (n, forms) => `${fmtInt(n)}${NB}${plural(n, forms)}`;

export const FORMS = {
  run: ['запуск', 'запуска', 'запусков'],
  agent: ['агент', 'агента', 'агентов'],
  step: ['шаг', 'шага', 'шагов'],
  call: ['вызов', 'вызова', 'вызовов'],
  delegation: ['делегирование', 'делегирования', 'делегирований'],
  token: ['токен', 'токена', 'токенов'],
  model: ['модель', 'модели', 'моделей'],
  server: ['сервер', 'сервера', 'серверов'],
  subordinate: ['подчинённый', 'подчинённых', 'подчинённых'],
  turn: ['ход', 'хода', 'ходов'],
  second: ['секунда', 'секунды', 'секунд'],
};

/** Context fill in percent (0..100+) or null when unknown. */
export function ctxPercent(used, window) {
  if (!isNum(used) || !isNum(window) || window <= 0) return null;
  return (used / window) * 100;
}
/** "48 200 / 200 000" */
export function fmtContext(used, window) {
  if (!isNum(window)) return DASH;
  return `${isNum(used) ? fmtInt(used) : DASH}${NB}/${NB}${fmtInt(window)}`;
}

/** "ДИ" for "Дирижёр", "ИС" for "Исследователь", "КР" for "Код ревью". */
export function initials(name) {
  const words = String(name || '').trim().split(/\s+/).filter(Boolean);
  if (!words.length) return '?';
  const letters = words.length === 1 ? Array.from(words[0]).slice(0, 2) : [Array.from(words[0])[0], Array.from(words[1])[0]];
  return letters.join('').toUpperCase();
}

export const shortId = (id, n = 8) => (id ? String(id).slice(0, n) : DASH);

export const RUNTIME_LABEL = { claude_code: 'Claude Code', opencode: 'OpenCode', demo: 'Демо' };
export const AGENT_STATUS_LABEL = { idle: 'Ожидает', working: 'Работает', delegating: 'Делегирует', error: 'Ошибка' };
export const RUN_STATUS_LABEL = { queued: 'В очереди', running: 'Выполняется', ok: 'Готово', error: 'Ошибка', cancelled: 'Отменён' };
export const STEP_KIND_LABEL = { message: 'Сообщение', thinking: 'Размышление', tool: 'Инструмент', delegation: 'Делегирование', system: 'Система', error: 'Ошибка' };
export const ORIGIN_LABEL = { user: 'Вручную', delegation: 'Делегирование', external: 'Терминал' };
export const PERMISSION_LABEL = { read: 'Только чтение', edit: 'Правка файлов', full: 'Полный доступ' };
export const SERVER_KIND_LABEL = { lmstudio: 'LM Studio', llamacpp: 'llama.cpp', openai_compat: 'OpenAI-совместимый', anthropic: 'Anthropic API' };
export const SERVER_STATE_LABEL = { up: 'Доступен', down: 'Не отвечает', unknown: 'Не проверен' };

export const runtimeLabel = (id) => RUNTIME_LABEL[id] || id || DASH;
export const agentStatusLabel = (s) => AGENT_STATUS_LABEL[s] || s || DASH;
export const runStatusLabel = (s) => RUN_STATUS_LABEL[s] || s || DASH;
