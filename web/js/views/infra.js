// Модели и железо: runtimes, model servers, hardware.
import { h } from '../core/dom.js';
import { api } from '../core/api.js';
import { store } from '../core/store.js';
import { fmtInt, fmtDec, fmtBytes, fmtPercent, fmtDuration, fmtRelative, fmtTokens, pl, SERVER_KIND_LABEL, SERVER_STATE_LABEL, runtimeLabel, NB } from '../core/format.js';
import { card, button, iconButton, badge, statusDot, runtimeMark, switchControl, field, input, select, modal, confirm, toast, emptyState, table, hoverCard, attachAgentHover, contextDial, icon } from '../ui/index.js';
import { lineChart, meter } from '../charts/index.js';
import { xicon } from '../runs/util.js';

let offs = [];
let timers = [];

const section = (title, sub, actions) => h('div.if__sect', h('div.if__sect-t', h('h2.if__h', title), sub ? h('p.if__sub', sub) : null), actions ? h('div.if__sect-a', actions) : null);
const ratio = (a, b) => (b > 0 && a != null ? (a / b) * 100 : null);

export default {
  id: 'infra',
  title: 'Модели и железо',
  layout: 'page',
  mount(root, ctx) {
    // =========================================================================
    // Runtimes
    // =========================================================================
    const rtGrid = h('div.if__rts');
    const refreshBtn = button({ label: 'Проверить снова', icon: 'refresh-cw', variant: 'secondary', size: 'sm', onClick: async () => {
      refreshBtn.update({ loading: true });
      try { const d = await api.post('/runtimes/refresh'); toast('Среды выполнения проверены', { kind: 'ok', timeout: 2500 }); if (d && d.runtimes) paintRuntimes(d.runtimes); }
      catch (e) { toast(e.message || 'Не удалось проверить', { kind: 'error' }); }
      refreshBtn.update({ loading: false });
    } });
    const rtCards = new Map();
    function paintRuntimes(list) {
      const keep = new Set();
      (list || []).forEach((r, i) => {
        keep.add(r.id);
        let c = rtCards.get(r.id);
        if (!c) {
          const dl = h('dl.if__facts');
          const note = h('p.if__note');
          const head = h('div.if__rt-head');
          const el = card({ padding: 'lg', className: 'if__rt', children: [head, dl, note] });
          c = { el, dl, note, head };
          rtCards.set(r.id, c);
        }
        c.head.replaceChildren(
          h('div.row.gap-2', runtimeMark({ runtime: r.id, size: 18 }), h('span.if__rt-name', r.label || runtimeLabel(r.id))),
          h('span.row.gap-2.if__state', statusDot({ status: r.available ? 'ok' : 'error' }), r.available ? 'Доступна' : 'Не найдена'));
        const models = (r.models || []).length;
        c.dl.replaceChildren(
          fact('Версия', r.version || '—'), fact('Путь', r.path || (r.id === 'demo' ? 'встроена' : '—'), true), fact('Моделей', models ? fmtInt(models) : '0'));
        c.note.textContent = r.available ? '' : (r.error || 'Среда не найдена');
        c.note.hidden = r.available;
        if (!r.available) c.note.dataset.kind = 'err'; else delete c.note.dataset.kind;
        if (rtGrid.children[i] !== c.el) rtGrid.insertBefore(c.el, rtGrid.children[i] || null);
      });
      for (const [id, c] of rtCards) if (!keep.has(id)) { c.el.remove(); rtCards.delete(id); }
      rtGrid.hidden = !(list && list.length);
    }
    function fact(label, value, mono = true) {
      return h('div.if__fact', h('dt', label), h('dd', { class: mono ? 'mono truncate' : 'truncate', title: String(value) }, value));
    }

    // =========================================================================
    // Servers
    // =========================================================================
    const srvList = h('div.if__servers');
    const srv = new Map(); // id -> controller

    function srvState(s) {
      if (!s.enabled) return { dot: 'idle', text: 'Выключен' };
      const st = s.status && s.status.state;
      return st === 'up' ? { dot: 'ok', text: SERVER_STATE_LABEL.up } : st === 'down' ? { dot: 'error', text: SERVER_STATE_LABEL.down } : { dot: 'idle', text: SERVER_STATE_LABEL.unknown };
    }

    function agentsUsing(modelId) {
      return store.get('agents').filter((a) => {
        const m = (a.live && a.live.model) || a.model;
        return m && (m === modelId || m.endsWith('/' + modelId) || modelId.endsWith('/' + m));
      });
    }
    function modelHover(m, server) {
      const node = h('div.if__hc');
      const render = () => {
        const users = agentsUsing(m.id);
        node.replaceChildren(
          h('div.if__hc-title.mono', m.id),
          h('div.if__hc-facts',
            hcRow('Контекст', m.context_window ? `${fmtInt(m.context_window)}${m.max_context_window ? NB + '/' + NB + fmtInt(m.max_context_window) : ''}` : (m.max_context_window ? 'не загружена, максимум ' + fmtInt(m.max_context_window) : 'нет данных')),
            m.state ? hcRow('Состояние', m.state === 'loaded' ? 'Загружена в память' : 'Доступна, не загружена') : null,
            m.arch ? hcRow('Архитектура', m.arch) : null,
            m.quantization ? hcRow('Квантизация', m.quantization) : null,
            m.size_bytes ? hcRow('Размер', fmtBytes(m.size_bytes)) : null,
            m.type ? hcRow('Тип', m.type) : null,
            hcRow('Сервер', server.name)),
          h('div.if__hc-sub', 'Используют агенты'),
          users.length ? h('ul.if__hc-users', users.map((a) => h('li',
            runtimeMark({ runtime: a.runtime, size: 14 }), h('span.if__hc-aname', a.name),
            h('span.if__hc-act', a.live && a.live.status !== 'idle' ? (a.live.activity || 'Работает') : 'Простаивает')))) : h('p.if__hc-none', 'Пока никто'));
      };
      render();
      return node;
    }
    const hcRow = (k, v) => h('div.if__hc-row', h('span', k), h('span.mono', v));

    function modelRow(server, m) {
      const nameEl = h('span.if__mname.mono.truncate');
      const stateEl = h('span.if__mstate');
      const ctxMeter = meter({ label: '', value: null, display: '—' });
      const ctxFig = h('span.if__mctx-fig.mono');
      const quant = h('span.mono.if__cell');
      const size = h('span.mono.if__cell.if__cell--r');
      const row = h('div.if__mrow', h('div.if__mcol-name', nameEl, stateEl), h('div.if__mcol-ctx', ctxMeter, ctxFig), quant, size);
      const ctl = { el: row };
      let current = m;
      ctl.update = (mm, srvObj) => {
        current = mm; server = srvObj;
        nameEl.textContent = mm.id; nameEl.title = mm.id;
        stateEl.textContent = mm.state === 'loaded' ? 'загружена' : mm.state === 'available' ? 'доступна' : '';
        stateEl.dataset.state = mm.state || '';
        const ok = mm.context_window && mm.max_context_window;
        ctxMeter.update({ value: mm.context_window || (mm.state === 'loaded' ? null : 0), max: mm.max_context_window || mm.context_window || 1, display: ' ', hint: '' });
        ctxFig.textContent = mm.context_window ? (mm.max_context_window ? `${fmtInt(mm.context_window)} / ${fmtInt(mm.max_context_window)}` : fmtInt(mm.context_window)) : (mm.max_context_window ? `до ${fmtInt(mm.max_context_window)}` : '—');
        quant.textContent = mm.quantization || mm.arch || '—';
        size.textContent = mm.size_bytes ? fmtBytes(mm.size_bytes) : '—';
      };
      ctl.update(m, server);
      hoverCard(row, () => modelHover(current, server), { placement: 'bottom-start', width: 360 });
      row.tabIndex = 0;
      return ctl;
    }

    function buildServer(s) {
      const ctl = { rows: new Map(), slotMeters: new Map() };
      const title = h('span.if__sname', s.name);
      const kindBadge = badge({ text: SERVER_KIND_LABEL[s.kind] || s.kind });
      kindBadge.hidden = s.name === (SERVER_KIND_LABEL[s.kind] || s.kind);
      const loc = badge({ text: s.location === 'cloud' ? 'облако' : 'локальный' });
      const url = h('span.if__url.mono.truncate');
      const dot = statusDot({ status: 'idle' });
      const stateText = h('span');
      const meta = h('span.if__smeta.mono');
      const sw = switchControl({ checked: s.enabled, ariaLabel: `Сервер «${s.name}» включён`, onChange: async (v) => {
        try { await api.patch(`/servers/${encodeURIComponent(ctl.id)}`, { enabled: v }); }
        catch (e) { toast(e.message || 'Не удалось изменить', { kind: 'error' }); sw.checked = !v; }
      } });
      const checkBtn = button({ label: 'Проверить', icon: 'refresh-cw', variant: 'secondary', size: 'sm', onClick: async () => {
        checkBtn.update({ loading: true });
        try { await api.post(`/servers/${encodeURIComponent(ctl.id)}/check`); } catch (e) { toast(e.message || 'Не удалось проверить', { kind: 'error' }); }
        checkBtn.update({ loading: false });
      } });
      const delBtn = iconButton({ icon: 'x', label: `Удалить сервер «${s.name}»`, onClick: async () => {
        const ok = await confirm({ title: `Удалить сервер «${ctl.server.name}»?`, message: 'Он пропадёт из списка. Сам сервер моделей не изменится.', confirmLabel: 'Удалить', danger: true });
        if (!ok) return;
        try { await api.del(`/servers/${encodeURIComponent(ctl.id)}`); toast('Сервер удалён', { kind: 'ok', timeout: 2500 }); }
        catch (e) { toast(e.message || 'Не удалось удалить', { kind: 'error' }); }
      } });
      const errEl = h('p.if__err');
      const modelsHead = h('div.if__mhead', h('span', 'Модель'), h('span', 'Контекст: загружен / максимум'), h('span', 'Квантизация'), h('span.if__cell--r', 'Размер'));
      const modelsEl = h('div.if__models');
      const slotsEl = h('div.if__slots');
      const metricsEl = h('div.if__metrics');
      const cloudNote = h('p.if__note');
      const body = h('div.if__sbody', errEl, cloudNote, modelsHead, modelsEl, slotsEl, metricsEl);
      const el = card({ padding: 'none', className: 'if__srv', children: [
        h('div.if__shead',
          h('div.if__sid', h('div.row.gap-2.wrap', title, kindBadge, loc), url),
          h('div.if__sstate', h('span.row.gap-2', dot, stateText), meta),
          h('div.if__sact', checkBtn, sw, s.builtin ? null : delBtn)),
        body] });
      Object.assign(ctl, { el, title, url, dot, stateText, meta, sw, errEl, modelsEl, modelsHead, slotsEl, metricsEl, cloudNote, body, delBtn });
      ctl.update = (srvObj) => {
        ctl.server = srvObj; ctl.id = srvObj.id;
        title.textContent = srvObj.name;
        kindBadge.hidden = srvObj.name === (SERVER_KIND_LABEL[srvObj.kind] || srvObj.kind);
        url.textContent = srvObj.base_url; url.title = srvObj.base_url;
        const st = srvState(srvObj);
        dot.update({ status: st.dot }); stateText.textContent = st.text;
        const status = srvObj.status;
        const bits = [];
        if (status && srvObj.enabled && status.latency_ms != null) bits.push(`${fmtInt(status.latency_ms)}${NB}мс`);
        if (status && status.version) bits.push('v' + status.version);
        if (status && status.checked_at) bits.push('проверен ' + fmtRelative(status.checked_at));
        meta.textContent = bits.join(' · ');
        sw.checked = srvObj.enabled;
        errEl.textContent = srvObj.enabled && status && status.error ? status.error : '';
        errEl.hidden = !errEl.textContent;
        // models
        const models = (status && status.models) || [];
        const keep = new Set();
        models.forEach((m, i) => {
          keep.add(m.id);
          let r = ctl.rows.get(m.id);
          if (!r) { r = modelRow(srvObj, m); ctl.rows.set(m.id, r); }
          else r.update(m, srvObj);
          if (modelsEl.children[i] !== r.el) modelsEl.insertBefore(r.el, modelsEl.children[i] || null);
        });
        for (const [id, r] of ctl.rows) if (!keep.has(id)) { r.el.remove(); ctl.rows.delete(id); }
        modelsEl.hidden = modelsHead.hidden = !models.length;
        cloudNote.textContent = srvObj.enabled && status && status.state === 'up' && !models.length
          ? (srvObj.kind === 'anthropic' ? 'Облачный сервер: проверяется только доступность, ключ не используется' : 'Сервер ответил, но моделей не показал') : '';
        cloudNote.hidden = !cloudNote.textContent;
        // llama.cpp slots + metrics
        const slots = (status && status.slots) || [];
        slotsEl.hidden = !slots.length;
        slotsEl.replaceChildren(...(slots.length ? [h('div.if__sub2', 'Слоты'), ...slots.map((sl) => {
          const m = meter({ label: `Слот ${sl.id}`, value: sl.context_used, max: sl.context_window || 1, display: sl.state === 'processing' ? 'обрабатывает' : 'свободен', hint: sl.context_window ? `${fmtInt(sl.context_used || 0)} / ${fmtInt(sl.context_window)}` : '' });
          return m;
        })] : []));
        const mt = status && status.metrics;
        metricsEl.hidden = !mt;
        metricsEl.replaceChildren(...(mt ? [
          mfig('Скорость', mt.tokens_per_s != null ? `${fmtDec(mt.tokens_per_s, 1)}${NB}ток/с` : '—'),
          mfig('В работе', mt.requests_processing != null ? fmtInt(mt.requests_processing) : '—'),
          mfig('В очереди', mt.requests_deferred != null ? fmtInt(mt.requests_deferred) : '—'),
          mfig('Токенов принято', fmtTokens(mt.prompt_tokens_total)),
          mfig('Токенов выдано', fmtTokens(mt.generated_tokens_total))] : []));
        body.hidden = !(errEl.textContent || models.length || slots.length || mt || cloudNote.textContent);
        el.classList.toggle('is-off', !srvObj.enabled);
      };
      ctl.update(s);
      return ctl;
    }
    const mfig = (k, v) => h('div.if__mfig', h('span.if__mfig-k', k), h('span.if__mfig-v.mono', v));

    function paintServers(list) {
      const keep = new Set();
      (list || []).forEach((s, i) => {
        keep.add(s.id);
        let c = srv.get(s.id);
        if (!c) { c = buildServer(s); srv.set(s.id, c); } else c.update(s);
        if (srvList.children[i] !== c.el) srvList.insertBefore(c.el, srvList.children[i] || null);
      });
      for (const [id, c] of srv) if (!keep.has(id)) { c.el.remove(); srv.delete(id); }
      srvEmpty.hidden = !!(list && list.length);
    }
    const srvEmpty = h('div', emptyState({ compact: true, title: 'Серверов моделей нет', text: 'Добавьте LM Studio, llama.cpp или другой сервер с совместимым API' }));

    // add server dialog
    function addServer() {
      const name = input({ placeholder: 'Например, Мой LM Studio', ariaLabel: 'Название' });
      const kind = select({ options: [{ value: 'lmstudio', label: 'LM Studio' }, { value: 'llamacpp', label: 'llama.cpp' }, { value: 'openai_compat', label: 'OpenAI-совместимый' }], value: 'lmstudio', ariaLabel: 'Тип' });
      const url = input({ placeholder: 'http://127.0.0.1:1234', mono: true, ariaLabel: 'Адрес' });
      const fName = field({ label: 'Название', control: name });
      const fKind = field({ label: 'Тип', control: kind });
      const fUrl = field({ label: 'Адрес', hint: 'Адрес сервера вместе с портом', control: url });
      const save = button({ label: 'Добавить', variant: 'primary', onClick: submit });
      const cancel = button({ label: 'Отмена', variant: 'secondary', onClick: () => m.close() });
      const m = modal({ title: 'Добавить сервер моделей', content: h('div.stack.gap-4', fName, fKind, fUrl), footer: [cancel, save], width: 460 });
      setTimeout(() => name.focus(), 50);
      async function submit() {
        fName.update({ error: name.value.trim() ? '' : 'Введите название' });
        const u = url.value.trim();
        fUrl.update({ error: /^https?:\/\/.+/i.test(u) ? '' : 'Адрес должен начинаться с http:// или https://' });
        if (!name.value.trim() || !/^https?:\/\/.+/i.test(u)) return;
        save.update({ loading: true });
        try { await api.post('/servers', { name: name.value.trim(), kind: kind.value, base_url: u }); toast('Сервер добавлен', { kind: 'ok', timeout: 2500 }); m.close(); }
        catch (e) { fUrl.update({ error: e.message || 'Не удалось добавить' }); }
        save.update({ loading: false });
      }
    }
    const addBtn = button({ label: 'Добавить сервер', icon: 'plus', variant: 'secondary', size: 'sm', onClick: addServer });

    // =========================================================================
    // Hardware
    // =========================================================================
    const cpuM = meter({ label: 'Процессор', value: null });
    const ramM = meter({ label: 'Память', value: null });
    const swapM = meter({ label: 'Подкачка', value: null });
    const gpuHost = h('div.if__gpus');
    const metersCard = card({ title: 'Нагрузка сейчас', padding: 'lg', className: 'if__meters', children: h('div.stack.gap-5', cpuM, ramM, swapM, gpuHost) });
    const chart = lineChart({ x: [], series: [{ id: 'cpu', label: 'Процессор', values: [] }, { id: 'ram', label: 'Память', values: [] }], height: 260, accent: 'cpu', yMax: 100, format: (v) => fmtPercent(v), formatAxis: (v) => fmtPercent(v), formatX: (t) => { const d = new Date(t); return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}:${String(d.getSeconds()).padStart(2, '0')}`; }, ariaLabel: 'Загрузка процессора и памяти за последние 10 минут' });
    const legend = h('ul.if__legend');
    const chartCard = card({ title: 'Загрузка за 10 минут', subtitle: 'Процессор и память, обновляется каждые 2 секунды', padding: 'lg', className: 'if__chart', children: [legend, chart] });
    const procTable = table({
      ariaLabel: 'ИИ-процессы', dense: true, rowKey: (r) => r.pid, rows: [],
      columns: [
        { key: 'kind', label: 'Вид', width: 150, render: (r) => h('span.row.gap-2', KIND_MARK[r.kind] ? runtimeMark({ runtime: KIND_MARK[r.kind] }) : icon('server', { size: 14 }), KIND_LABEL[r.kind] || r.kind) },
        { key: 'name', label: 'Процесс', render: (r) => h('span.truncate', r.name) },
        { key: 'pid', label: 'PID', align: 'right', mono: true, width: 90, render: (r) => String(r.pid) },
        { key: 'cpu', label: 'Процессор', align: 'right', mono: true, width: 110, render: (r) => fmtPercent(r.cpu_percent) },
        { key: 'mem', label: 'Память', align: 'right', mono: true, width: 110, render: (r) => fmtBytes(r.rss_bytes) },
        { key: 'vram', label: 'Видеопамять', align: 'right', mono: true, width: 120, render: (r) => (r.gpu_mem_bytes != null ? fmtBytes(r.gpu_mem_bytes) : '—') },
      ],
      empty: emptyState({ compact: true, title: 'ИИ-процессов не видно', text: 'Claude Code, OpenCode, LM Studio и llama.cpp появятся здесь, когда запущены' }),
    });
    const procCard = card({ title: 'ИИ-процессы', subtitle: 'Что из этого сейчас запущено на машине', padding: 'none', children: procTable });
    const KIND_MARK = { claude_code: 'claude_code', opencode: 'opencode' };
    const KIND_LABEL = { claude_code: 'Claude Code', opencode: 'OpenCode', lmstudio: 'LM Studio', llamacpp: 'llama.cpp', ollama: 'Ollama' };

    const gpuCtl = new Map();
    const noGpu = h('div.if__nogpu', icon('cpu', { size: 16 }), h('div', h('div.if__nogpu-t', 'Видеокарта не обнаружена'), h('div.if__nogpu-s', 'Показатели появятся, когда на машине будет видеокарта с работающим драйвером')));
    function paintGpus(gpus) {
      if (!gpus || !gpus.length) { gpuCtl.clear(); gpuHost.replaceChildren(noGpu); return; }
      const keep = new Set();
      gpus.forEach((g, i) => {
        keep.add(g.index);
        let c = gpuCtl.get(g.index);
        if (!c) {
          c = { util: meter({ label: 'Загрузка', value: null }), vram: meter({ label: 'Видеопамять', value: null }), temp: meter({ label: 'Температура', value: null }), power: h('div.if__power', h('span', 'Мощность'), h('span.mono')) };
          c.name = h('div.if__gpuname');
          c.el = h('div.if__gpu', c.name, c.util, c.vram, c.temp, c.power);
          gpuCtl.set(g.index, c);
        }
        c.name.textContent = g.name || `Видеокарта ${g.index}`;
        c.util.update({ value: g.util_percent, max: 100 });
        c.vram.update({ value: g.mem_used_bytes, max: g.mem_total_bytes, display: g.mem_total_bytes ? fmtPercent(ratio(g.mem_used_bytes, g.mem_total_bytes)) : '—', hint: g.mem_total_bytes ? `${fmtBytes(g.mem_used_bytes)} из ${fmtBytes(g.mem_total_bytes)}` : '' });
        c.temp.update({ value: g.temp_c, max: 100, display: g.temp_c != null ? `${fmtInt(g.temp_c)}${NB}°C` : '—' });
        c.power.lastChild.textContent = g.power_w != null ? `${fmtInt(g.power_w)}${NB}Вт` : '—';
        if (gpuHost.children[i] !== c.el) gpuHost.insertBefore(c.el, gpuHost.children[i] || null);
      });
      for (const [k, c] of gpuCtl) if (!keep.has(k)) { c.el.remove(); gpuCtl.delete(k); }
      if (noGpu.parentNode === gpuHost) noGpu.remove();
    }
    function paintHardware(hw) {
      if (!hw) { cpuM.update({ value: null }); ramM.update({ value: null }); return; }
      cpuM.update({ value: hw.cpu_percent, max: 100, hint: `${fmtInt(hw.cpu_count)} ${pl(hw.cpu_count, ['ядро', 'ядра', 'ядер']).replace(/^\d+\s*/, '')}` });
      ramM.update({ value: hw.ram_used_bytes, max: hw.ram_total_bytes, display: fmtPercent(ratio(hw.ram_used_bytes, hw.ram_total_bytes)), hint: `${fmtBytes(hw.ram_used_bytes)} из ${fmtBytes(hw.ram_total_bytes)}` });
      if (hw.swap_total_bytes > 0) swapM.update({ value: hw.swap_used_bytes, max: hw.swap_total_bytes, display: fmtPercent(ratio(hw.swap_used_bytes, hw.swap_total_bytes)), hint: `${fmtBytes(hw.swap_used_bytes)} из ${fmtBytes(hw.swap_total_bytes)}` });
      else swapM.update({ value: null, display: 'нет', hint: 'подкачка не настроена' });
      paintGpus(hw.gpus);
      if (hw.processes) procTable.update({ rows: hw.processes.slice().sort((a, b) => (b.cpu_percent || 0) - (a.cpu_percent || 0)) });
    }
    function paintSeries() {
      const all = store.get('hardwareSeries') || [];
      const last = all.length ? all[all.length - 1].ts : Date.now();
      const s = all.filter((p) => p.ts >= last - 600000);
      const hasGpu = s.some((p) => p.gpus && p.gpus.length);
      const series = [
        { id: 'cpu', label: 'Процессор', values: s.map((p) => p.cpu_percent) },
        { id: 'ram', label: 'Память', values: s.map((p) => ratio(p.ram_used_bytes, p.ram_total_bytes)) },
      ];
      if (hasGpu) {
        series.push({ id: 'gpu', label: 'Видеокарта', values: s.map((p) => (p.gpus && p.gpus[0] && p.gpus[0].util_percent) || 0) });
        series.push({ id: 'vram', label: 'Видеопамять', values: s.map((p) => (p.gpus && p.gpus[0] ? ratio(p.gpus[0].mem_used_bytes, p.gpus[0].mem_total_bytes) || 0 : 0)) });
      }
      if (series.length !== lastSeriesCount) {
        lastSeriesCount = series.length;
        legend.replaceChildren(...series.map((q, i) => h('li', h('span.if__sw', { style: { background: i === 0 ? 'var(--accent)' : ['var(--ink-1)', 'var(--ink-2)', 'var(--ink-3)'][Math.min(i - 1, 2)] } }), q.label)));
      }
      chart.update({ x: s.map((p) => p.ts), series });
    }
    let lastSeriesCount = 0;

    // =========================================================================
    root.replaceChildren(h('div.if',
      section('Среды выполнения', 'Чем агенты запускаются на этой машине', refreshBtn), rtGrid,
      section('Серверы моделей', 'Локальные и облачные, проверяются каждые 10 секунд', addBtn), srvList, srvEmpty,
      section('Железо', 'Нагрузка машины и процессы, связанные с ИИ'),
      h('div.if__hw', metersCard, chartCard), procCard));

    paintRuntimes(store.get('runtimes'));
    paintServers(store.get('servers'));
    paintHardware(store.get('hardware'));
    store.loadHardwareHistory(600).then(paintSeries).catch(() => paintSeries());
    paintSeries();
    offs.push(store.subscribe('runtimes', paintRuntimes));
    offs.push(store.subscribe('servers', paintServers));
    offs.push(store.subscribe('hardware', paintHardware));
    offs.push(store.subscribe('hardwareSeries', paintSeries));
    offs.push(store.subscribe('agents', () => { /* hover cards read agents when opened */ }));
    // relative check times drift; refresh them every 15 s
    timers.push(setInterval(() => { for (const c of srv.values()) c.update(c.server); }, 15000));
    // fresh runtime info (the store holds the list from /state)
    api.get('/runtimes').then((d) => { if (d && d.runtimes) paintRuntimes(d.runtimes); }).catch(() => {});
  },
  unmount() { offs.forEach((f) => f()); offs = []; timers.forEach(clearInterval); timers = []; },
};
