// Step list (a trace log of the whole delegation tree) and the step / result detail.
import { h } from '../core/dom.js';
import { fmtDuration, fmtClock, STEP_KIND_LABEL, runStatusLabel } from '../core/format.js';
import { codeBlock, button, statusDot, runtimeMark, emptyState, icon } from '../ui/index.js';
import { router } from '../core/router.js';
import { prettyJson, fmtOffset, DOT, runAgentName } from './util.js';

const kindClass = (s) => `tl-k--${s.kind === 'error' || s.status === 'error' ? 'error' : s.kind}`;

export function createStepList({ onSelect }) {
  const rows = new Map();
  const ul = h('ul.sl__list', { role: 'listbox', 'aria-label': 'Шаги запуска' });
  const empty = h('div.sl__empty', 'Шагов пока нет');
  const el = h('div.sl', ul, empty);
  const S = { selected: null, current: null, filterRun: null, origin: 0, stick: true, steps: [], runs: new Map() };
  ul.addEventListener('scroll', () => { S.stick = ul.scrollTop + ul.clientHeight >= ul.scrollHeight - 24; });

  function makeRow(s) {
    const sw = h('span', { class: 'sl__sw' });
    const title = h('span.sl__title.truncate');
    const meta = h('span.sl__meta.truncate');
    const dur = h('span.sl__dur.mono');
    const btn = h('button', { type: 'button', class: 'sl__row', role: 'option', onclick: () => onSelect(btn.__step) },
      sw, h('span.sl__text', title, meta), dur);
    btn.__sw = sw; btn.__title = title; btn.__meta = meta; btn.__dur = dur;
    return btn;
  }

  function paintRow(btn, s) {
    btn.__step = s;
    btn.__sw.className = `sl__sw ${kindClass(s)}` + (s.status === 'running' ? ' is-run' : '');
    btn.__title.textContent = s.title;
    const run = S.runs.get(s.run_id);
    const bits = [];
    if (S.multi && run) bits.push(runAgentName(run));
    bits.push(STEP_KIND_LABEL[s.kind] || s.kind);
    bits.push('+' + fmtOffset(s.started_at - S.origin));
    btn.__meta.textContent = bits.join(' · ');
    btn.__dur.textContent = s.status === 'running' ? 'идёт' : fmtDuration(s.duration_ms != null ? s.duration_ms : (s.ended_at != null ? s.ended_at - s.started_at : null));
    btn.dataset.status = s.status;
    btn.style.paddingLeft = (S.multi && run ? 12 + run.depth * 12 : 12) + 'px';
  }

  function render() {
    const list = S.steps.filter((s) => !S.filterRun || s.run_id === S.filterRun)
      .sort((a, b) => (a.started_at - b.started_at) || (a.seq - b.seq));
    const keep = new Set();
    list.forEach((s, i) => {
      keep.add(s.id);
      let r = rows.get(s.id);
      if (!r) { r = makeRow(s); rows.set(s.id, r); }
      paintRow(r, s);
      r.classList.toggle('is-sel', s.id === S.selected);
      r.setAttribute('aria-selected', s.id === S.selected ? 'true' : 'false');
      r.classList.toggle('is-current', s.id === S.current);
      r.classList.toggle('is-hidden', !!(S.hidden && S.hidden.has(s.id)));
      const li = r.parentNode && r.parentNode.tagName === 'LI' ? r.parentNode : null;
      if (!li) { const l = h('li', r); r.__li = l; }
      if (ul.children[i] !== r.__li) ul.insertBefore(r.__li, ul.children[i] || null);
    });
    for (const [id, r] of rows) if (!keep.has(id)) { r.__li.remove(); rows.delete(id); }
    empty.hidden = list.length > 0;
    if (S.stick && S.autoStick) ul.scrollTop = ul.scrollHeight;
  }

  return {
    el,
    setData({ runs, steps, origin, live }) {
      S.runs = new Map(runs.map((r) => [r.id, r]));
      S.multi = runs.length > 1;
      S.steps = steps; S.origin = origin; S.autoStick = live;
      render();
    },
    setFilter(runId) { S.filterRun = runId; render(); },
    /** hide steps after the playhead during replay; ids is a Set of hidden step ids or null */
    setHidden(ids) { S.hidden = ids; render(); },
    setSelected(id) { S.selected = id; for (const [k, r] of rows) { r.classList.toggle('is-sel', k === id); r.setAttribute('aria-selected', k === id ? 'true' : 'false'); } },
    setCurrent(id, follow) {
      if (S.current === id) return;
      S.current = id;
      for (const [k, r] of rows) r.classList.toggle('is-current', k === id);
      const r = rows.get(id);
      if (r && follow) {
        const top = r.__li.offsetTop, bottom = top + r.__li.offsetHeight;
        if (top < ul.scrollTop) ul.scrollTop = top - 8;
        else if (bottom > ul.scrollTop + ul.clientHeight) ul.scrollTop = bottom - ul.clientHeight + 8;
      }
    },
  };
}

export function createDetail({ onOpenLane }) {
  const host = h('div.sd');
  const S = { stepId: null, in: null, out: null, outPre: null };

  function label(text) { return h('div.sd__label', text); }

  function buildStep(step, run, child) {
    S.in = null; S.out = null;
    const meta = h('div.sd__meta');
    const head = h('div.sd__head', h('h3.sd__title', step.title), meta);
    const kids = [head];
    const metaBits = () => {
      const dur = step.status === 'running' ? 'идёт' : fmtDuration(step.duration_ms);
      meta.replaceChildren(
        h('span.sd__chip', h('span', { class: `sl__sw ${kindClass(step)}` }), STEP_KIND_LABEL[step.kind] || step.kind),
        h('span.row.gap-1', statusDot({ status: step.status === 'running' ? 'working' : step.status === 'error' ? 'error' : 'ok' }), step.status === 'running' ? 'Выполняется' : step.status === 'error' ? 'Ошибка' : 'Готово'),
        run ? h('span.row.gap-1', runtimeMark({ runtime: run.runtime, size: 14 }), runAgentName(run)) : null,
        h('span.mono', fmtClock(step.started_at)),
        h('span.mono', dur));
    };
    S.meta = metaBits;
    metaBits();
    if (step.kind === 'delegation') {
      kids.push(h('div.sd__deleg',
        child ? button({ label: `Показать дорожку «${child.agent_name}»`, variant: 'secondary', size: 'sm', icon: 'git-branch', onClick: () => onOpenLane(child.id) }) : h('span.muted', 'Дорожка подчинённого ещё не появилась'),
        child ? h('a.sd__link', { href: router.href('/runs/' + child.id) }, 'Страница запуска', icon('arrow-right', { size: 14 })) : null));
    }
    const hasIn = step.input && typeof step.input === 'object' && Object.keys(step.input).length;
    if (hasIn) {
      S.in = codeBlock({ code: prettyJson(step.input), maxHeight: 220 });
      S.inCode = prettyJson(step.input);
      kids.push(label('Вход'), S.in);
    }
    S.out = codeBlock({ code: step.output || '', maxHeight: 340, copy: true });
    S.outHolder = h('div.sd__out');
    S.outNote = h('div.sd__note');
    kids.push(label(step.kind === 'message' || step.kind === 'thinking' ? 'Текст' : 'Результат'), S.out, S.outNote);
    S.outCode = step.output || '';
    S.stepId = step.id;
    S.kind = 'step';
    host.replaceChildren(...kids);
    note(step);
  }

  function note(step) {
    S.outNote.textContent = step.output ? (step.status === 'running' ? 'Текст приходит, пока шаг идёт' : '') : (step.status === 'running' ? 'Вывода пока нет' : 'Шаг ничего не вернул');
    S.out.hidden = !step.output;
  }

  return {
    el: host,
    showStep(step, run, child) {
      if (S.kind !== 'step' || S.stepId !== step.id) { buildStep(step, run, child); return; }
      // update in place: streaming text stays where the reader scrolled
      S.meta();
      const out = step.output || '';
      if (out !== S.outCode) {
        const pre = S.out.querySelector('.code__pre');
        const atEnd = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 20;
        S.out.update(out); S.outCode = out;
        if (atEnd && step.status === 'running') pre.scrollTop = pre.scrollHeight;
      }
      note(step);
    },
    showResult(run) {
      S.kind = 'result'; S.stepId = null;
      const kids = [];
      if (run.error) kids.push(label('Ошибка'), h('div.sd__error', run.error));
      if (run.result) kids.push(h('div.sd__result', run.result));
      if (!kids.length) kids.push(emptyState({ compact: true, title: run.status === 'running' || run.status === 'queued' ? 'Итог появится, когда запуск закончится' : 'Итога нет', text: 'Выберите шаг на шкале или в списке, чтобы увидеть его вход и результат' }));
      host.replaceChildren(h('h3.sd__title', 'Итог запуска'), ...kids);
    },
    clear() { S.kind = null; S.stepId = null; host.replaceChildren(); },
  };
}
