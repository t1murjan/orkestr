// HTTP client for /api/v1. Errors become ApiError with .code and a Russian .message.
export const BASE = '/api/v1';

const FALLBACK = {
  not_found: 'Не найдено',
  validation: 'Некорректные данные',
  unknown_agent: 'Агент не найден',
  self_edge: 'Нельзя связать агента с самим собой',
  duplicate_edge: 'Такая связь уже есть',
  cycle: 'Связь создаст цикл в иерархии',
  name_taken: 'Это имя уже занято',
  runtime_unavailable: 'Среда выполнения недоступна',
  bad_cwd: 'Папка недоступна',
  depth_exceeded: 'Превышена глубина делегирования',
  empty_task: 'Задача не может быть пустой',
  builtin: 'Встроенный элемент нельзя удалить',
  network: 'Нет связи с сервером',
  timeout: 'Сервер не ответил вовремя',
  server: 'Ошибка сервера',
  bad_response: 'Сервер вернул непонятный ответ',
  aborted: 'Запрос отменён',
};

export class ApiError extends Error {
  constructor(code, message, status = 0, details = null) {
    super(message || FALLBACK[code] || FALLBACK.server);
    this.name = 'ApiError';
    this.code = code;
    this.status = status;
    this.details = details;
  }
}

function toQuery(params) {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params || {})) {
    if (v == null || v === '') continue;
    q.set(k, typeof v === 'boolean' ? (v ? 'true' : 'false') : String(v));
  }
  const s = q.toString();
  return s ? `?${s}` : '';
}

function parseError(status, body) {
  if (body && body.error && typeof body.error === 'object') {
    return new ApiError(body.error.code || 'server', body.error.message, status, body.error);
  }
  if (body && Array.isArray(body.detail)) { // FastAPI default validation shape
    const first = body.detail[0];
    const where = first && Array.isArray(first.loc) ? first.loc.filter((p) => p !== 'body').join('.') : '';
    return new ApiError('validation', where ? `${FALLBACK.validation}: ${where}` : FALLBACK.validation, status, body.detail);
  }
  if (status === 404) return new ApiError('not_found', null, status);
  if (status === 422) return new ApiError('validation', null, status);
  return new ApiError('server', null, status);
}

async function request(method, path, data, { signal, timeout = 20000 } = {}) {
  let url = BASE + (path.startsWith('/') ? path : '/' + path);
  const init = { method, headers: { Accept: 'application/json' }, signal };
  if (method === 'GET' || method === 'DELETE') url += toQuery(data);
  else if (data !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(data);
  }
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort('timeout'), timeout);
  if (signal) {
    if (signal.aborted) ctl.abort(signal.reason); else signal.addEventListener('abort', () => ctl.abort(signal.reason), { once: true });
  }
  init.signal = ctl.signal;
  let res;
  try {
    res = await fetch(url, init);
  } catch (e) {
    clearTimeout(timer);
    if (ctl.signal.aborted) throw new ApiError(ctl.signal.reason === 'timeout' ? 'timeout' : 'aborted');
    throw new ApiError('network');
  }
  clearTimeout(timer);
  let body = null;
  const raw = await res.text();
  if (raw) {
    try { body = JSON.parse(raw); } catch (e) { body = null; }
  }
  if (!res.ok) throw parseError(res.status, body);
  if (raw && body === null) throw new ApiError('bad_response', null, res.status);
  return body;
}

/** api.get('/runs', { status: 'running' }); api.post('/agents', {...}); api.patch(path, partial); api.del(path) */
export const api = {
  get: (path, params, opts) => request('GET', path, params, opts),
  post: (path, body, opts) => request('POST', path, body === undefined ? {} : body, opts),
  patch: (path, body, opts) => request('PATCH', path, body, opts),
  del: (path, params, opts) => request('DELETE', path, params, opts),
  ApiError,
};
export default api;
