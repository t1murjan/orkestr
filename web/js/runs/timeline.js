// The run timeline: a delegation tree drawn like a logic-analyser trace.
// One lane per run (depth-first, indented), time on X, a bar per step, thin links from a delegation step down to the child lane.
import { h, svg } from '../core/dom.js';
import { reducedMotion } from '../core/motion.js';
import { fmtDuration, fmtClock, STEP_KIND_LABEL, runtimeLabel } from '../core/format.js';
import { runtimeMark, statusDot, iconButton, attachAgentHover } from '../ui/index.js';
import { laneOrder, tickStep, fmtOffset, clamp, xicon, DOT, runAgentName } from './util.js';


const AXIS_H = 28;
const PAD_L = 10, PAD_R = 20;
const LANE_PAD = 9, ROW_H = 18, BAR_H = 12, MIN_LANE = 38;
const KINDS = ['tool', 'message', 'thinking', 'delegation', 'error'];

export function createTimeline({ onSelectStep, onSelectLane, onSeek } = {}) {
  const S = {
    runs: [], steps: [], lanes: [], laneTop: new Map(), totalH: AXIS_H, bars: new Map(), starts: new Map(),
    selStep: null, selLane: null, playhead: null, mode: 'fit', d0: 0, d1: 1, width: 600,
    live: false, dirty: true, raf: 0, last: 0, drag: null, seeking: false, destroyed: false, now: Date.now(),
  };

  const gGrid = svg('g.tl__grid');
  const gLanes = svg('g.tl__lanes');
  const gLinks = svg('g.tl__links');
  const gBars = svg('g.tl__bars');
  const gHead = svg('g.tl__head');
  const headLine = svg('line.tl__playline');
  const headChip = svg('g.tl__chip', svg('rect', { rx: 4, height: 18, y: 5 }), svg('text', { y: 18 }));
  gHead.append(headLine, headChip);
  const svgEl = svg('svg.tl__svg', { role: 'img', tabindex: 0, 'aria-label': 'Временная шкала запуска: дорожки агентов и шаги. Список шагов ниже содержит то же самое текстом.' }, gLanes, gGrid, gLinks, gBars, gHead);
  const labels = h('div.tl__labels');
  const axisSpacer = h('div.tl__axis-spacer');
  const body = h('div.tl__body', h('div.tl__labelcol', axisSpacer, labels), h('div.tl__plot', svgEl));
  const tip = h('div.tl__tip', { role: 'presentation' });
  tip.hidden = true;

  const legend = h('ul.tl__legend', { 'aria-label': 'Обозначения' }, [
    ['tool', 'Инструмент'], ['message', 'Сообщение'], ['thinking', 'Размышление'], ['delegation', 'Делегирование'], ['error', 'Ошибка'],
  ].map(([k, label]) => h('li', h('span', { class: `tl__sw tl-k--${k}` }), label)));
  legend.append(h('li', h('span.tl__sw.tl-k--run'), 'Идёт сейчас'));

  const tools = h('div.tl__tools',
    iconButton({ icon: 'minus', label: 'Уменьшить масштаб', size: 'sm', onClick: () => zoomAt(1.5, centerTime()) }),
    iconButton({ icon: 'plus', label: 'Увеличить масштаб', size: 'sm', onClick: () => zoomAt(1 / 1.5, centerTime()) }),
    h('button.tl__fit', { type: 'button', onclick: () => fit() }, xicon('maximize', 14), 'Вписать'));
  const bar = h('div.tl__bar', legend, tools);
  const el = h('div.tl', bar, body, tip, h('p.tl__hint', 'Ctrl и колесо мыши — масштаб, перетаскивание — сдвиг по времени, клик по шкале — перемотка'));

  // ---- geometry -----------------------------------------------------------
  const plotW = () => Math.max(40, S.width - PAD_L - PAD_R);
  const xOf = (t) => PAD_L + ((t - S.d0) / (S.d1 - S.d0)) * plotW();
  const tOf = (x) => S.d0 + ((x - PAD_L) / plotW()) * (S.d1 - S.d0);
  const centerTime = () => (S.d0 + S.d1) / 2;
  const nowT = () => S.now;

  function contentRange() {
    let start = Infinity, end = -Infinity;
    for (const { run } of S.lanes) {
      if (run.started_at != null) start = Math.min(start, run.started_at);
      const e = run.ended_at != null ? run.ended_at : (isRunning(run) ? nowT() : null);
      if (e != null) end = Math.max(end, e);
    }
    for (const s of S.steps) {
      if (s.started_at != null) start = Math.min(start, s.started_at);
      if (s.ended_at != null) end = Math.max(end, s.ended_at);
    }
    if (!Number.isFinite(start)) { start = nowT(); end = start + 1000; }
    if (!Number.isFinite(end) || end <= start) end = start + 1000;
    return { start, end };
  }
  const isRunning = (r) => r.status === 'running' || r.status === 'queued';

  function applyFit() {
    const { start, end } = contentRange();
    S.rootStart = start;
    if (S.live) {
      const el_ = Math.max(end - start, 1);
      const span = Math.max(15000, el_ * 1.04);
      S.d0 = start; S.d1 = start + span;
    } else { S.d0 = start; S.d1 = start + Math.max(end - start, 500); }
  }

  function setMode(m) { S.mode = m; }
  function fit() { setMode('fit'); S.dirty = true; kick(); }
  function zoomAt(f, ta) {
    setMode('manual');
    const span = (S.d1 - S.d0) * f;
    const { start, end } = contentRange();
    const clampedSpan = clamp(span, 40, Math.max(60000, (end - start) * 6));
    const k = clampedSpan / (S.d1 - S.d0);
    S.d0 = ta - (ta - S.d0) * k; S.d1 = ta + (S.d1 - ta) * k;
    S.dirty = true; kick();
  }
  function panBy(ms) { setMode('manual'); S.d0 += ms; S.d1 += ms; S.dirty = true; kick(); }

  // ---- layout (lanes, rows) -------------------------------------------------
  function layout() {
    S.lanes = laneOrder(S.runs);
    const byRun = new Map();
    for (const s of S.steps) { if (!byRun.has(s.run_id)) byRun.set(s.run_id, []); byRun.get(s.run_id).push(s); }
    let y = AXIS_H;
    S.laneTop = new Map();
    S.stepRow = new Map();
    S.stepById = new Map();
    for (const lane of S.lanes) {
      const list = (byRun.get(lane.run.id) || []).slice().sort((a, b) => (a.started_at - b.started_at) || (a.seq - b.seq));
      const rowEnds = [];
      for (const s of list) {
        let r = rowEnds.findIndex((e) => e <= s.started_at + 1);
        if (r < 0) { r = rowEnds.length; rowEnds.push(0); }
        rowEnds[r] = s.ended_at != null ? s.ended_at : Infinity;
        S.stepRow.set(s.id, r);
        S.stepById.set(s.id, s);
      }
      lane.steps = list;
      lane.rows = Math.max(1, rowEnds.length);
      lane.h = Math.max(MIN_LANE, LANE_PAD * 2 + BAR_H + (lane.rows - 1) * ROW_H);
      lane.top = y;
      S.laneTop.set(lane.run.id, lane);
      y += lane.h;
    }
    S.totalH = y + 4;
    svgEl.setAttribute('height', S.totalH);
    // lane backgrounds
    gLanes.replaceChildren(...S.lanes.map((l) => svg('g', { dataset: { lane: l.run.id } },
      svg('rect.tl__lanebg', { x: 0, y: l.top, width: '100%', height: l.h, dataset: { lane: l.run.id } }),
      svg('line.tl__sep', { x1: 0, x2: '100%', y1: l.top + l.h, y2: l.top + l.h }))));
    S.laneBg = new Map(S.lanes.map((l, i) => [l.run.id, gLanes.children[i].firstChild]));
    // baselines + links
    S.base = new Map();
    for (const l of S.lanes) {
      const line = svg('line.tl__base', { y1: l.top + LANE_PAD + BAR_H / 2, y2: l.top + LANE_PAD + BAR_H / 2 });
      S.base.set(l.run.id, line);
    }
    S.links = [];
    for (const l of S.lanes) {
      const r = l.run;
      const parentStep = r.parent_step_id && S.stepById.get(r.parent_step_id);
      const parentLane = parentStep && S.laneTop.get(parentStep.run_id);
      if (!parentLane) continue;
      const yb = parentLane.top + LANE_PAD + (S.stepRow.get(parentStep.id) || 0) * ROW_H + BAR_H;
      const yc = l.top + LANE_PAD + BAR_H / 2;
      S.links.push({ run: r, step: parentStep, yb, yc, down: svg('path.tl__link'), back: svg('path.tl__link.tl__link--back') });
    }
    gLinks.replaceChildren(...[...S.base.values(), ...S.links.flatMap((k) => [k.back, k.down])]);
    // bars: keep elements by step id
    const keep = new Set();
    for (const l of S.lanes) {
      for (const s of l.steps) {
        keep.add(s.id);
        let b = S.bars.get(s.id);
        if (!b) {
          const rect = svg('rect.tl__rect', { rx: 3, height: BAR_H, dataset: { step: s.id } });
          const text = svg('text.tl__label', { y: BAR_H / 2 + 4 });
          const g = svg('g.tl__step', rect, text);
          b = { g, rect, text, shown: '' };
          S.bars.set(s.id, b);
        }
        b.step = s;
        b.y = l.top + LANE_PAD + (S.stepRow.get(s.id) || 0) * ROW_H;
        b.g.setAttribute('transform', `translate(0 ${b.y})`);
        b.rect.dataset.step = s.id;
        b.rect.setAttribute('class', `tl__rect tl-k--${s.kind === 'error' ? 'error' : s.kind}`);
        gBars.append(b.g);
      }
    }
    for (const [id, b] of S.bars) if (!keep.has(id)) { b.g.remove(); S.bars.delete(id); }
    // labels
    labels.style.height = S.totalH - AXIS_H + 'px';
    const sig = S.lanes.map((l) => `${l.run.id}:${l.depth}:${l.run.status}:${l.top}:${l.h}`).join('|');
    if (sig !== S.labelSig) {
    S.labelSig = sig;
    S.labelEls = new Map();
    labels.replaceChildren(...S.lanes.map((l) => {
      const r = l.run;
      const dot = statusDot({ status: DOT[r.status] || 'idle' });
      const btn = h('button', { type: 'button', class: 'tl__lane', style: { top: l.top - AXIS_H + 'px', height: l.h + 'px', paddingLeft: 12 + l.depth * 14 + 'px' }, dataset: { run: r.id },
        title: r.task, onclick: () => onSelectLane && onSelectLane(r.id) },
        l.depth ? h('span.tl__elbow', { 'aria-hidden': 'true' }) : null,
        runtimeMark({ runtime: r.runtime, size: 14 }),
        h('span.tl__name.truncate', runAgentName(r)),
        h('span.tl__dot', dot));
      if (r.agent_id) attachAgentHover(btn.querySelector('.tl__name'), r.agent_id);
      S.labelEls.set(r.id, btn);
      return btn;
    }));
    }
    syncSelection();
    S.dirty = true;
  }

  function syncSelection() {
    for (const [id, b] of S.bars) b.rect.classList.toggle('is-sel', id === S.selStep);
    for (const [id, btn] of S.labelEls || []) btn.classList.toggle('is-sel', id === S.selLane);
    for (const [id, bg] of S.laneBg || []) bg.classList.toggle('is-sel', id === S.selLane);
  }

  // ---- drawing ----------------------------------------------------------------
  function render() {
    S.now = Date.now();
    S.width = svgEl.parentNode.clientWidth || S.width;
    if (S.mode === 'fit') applyFit();
    const W = plotW();
    const span = S.d1 - S.d0;
    const head = S.playhead;
    const inRange = (t) => t >= S.d0 - span && t <= S.d1 + span;
    const xr = (t) => xOf(t);

    // grid + axis
    const step = tickStep(span / W);
    const origin = S.rootStart != null ? S.rootStart : S.d0;
    const first = Math.ceil((S.d0 - origin) / step) * step;
    const ticks = [];
    for (let t = first; origin + t <= S.d1 + 1; t += step) {
      const x = xr(origin + t);
      if (x < PAD_L - 1 || x > S.width - 56) continue;
      ticks.push(svg('line.tl__tick', { x1: x, x2: x, y1: AXIS_H - 6, y2: S.totalH }));
      ticks.push(svg('text.tl__axis', { x: x + 4, y: AXIS_H - 10 }, fmtOffset(t)));
    }
    ticks.push(svg('line.tl__axisline', { x1: 0, x2: '100%', y1: AXIS_H, y2: AXIS_H }));
    gGrid.replaceChildren(...ticks);

    const live = S.live && head == null;
    // bars
    for (const [id, b] of S.bars) {
      const s = b.step;
      if (s.started_at == null || (head != null && s.started_at > head)) { b.g.style.display = 'none'; continue; }
      let end = s.ended_at != null ? s.ended_at : (s.status === 'running' ? nowT() : s.started_at);
      if (head != null) end = Math.min(end, head);
      const x0 = xr(s.started_at), x1 = xr(end);
      if (x1 < -20 || x0 > S.width + 20) { b.g.style.display = 'none'; continue; }
      b.g.style.display = '';
      const w = Math.max(2, x1 - x0);
      b.rect.setAttribute('x', x0.toFixed(1));
      b.rect.setAttribute('width', w.toFixed(1));
      const running = (s.status === 'running' && head == null) || (head != null && s.ended_at != null && s.ended_at > head && s.status !== 'error');
      b.rect.classList.toggle('is-run', running);
      b.rect.classList.toggle('is-err', s.status === 'error' || s.kind === 'error');
      let txt = '';
      if (w >= 54) { const n = Math.floor((w - 10) / 6.1); txt = s.title.length > n ? s.title.slice(0, Math.max(1, n - 1)) + '…' : s.title; }
      if (txt !== b.shown) { b.text.textContent = txt; b.shown = txt; }
      b.text.setAttribute('x', (Math.max(x0, 0) + 6).toFixed(1));
    }
    // baselines + links
    for (const l of S.lanes) {
      const r = l.run, line = S.base.get(r.id);
      const started = r.started_at != null && (head == null || r.started_at <= head);
      if (!started) { line.style.display = 'none'; continue; }
      let end = r.ended_at != null ? r.ended_at : nowT();
      if (head != null) end = Math.min(end, head);
      line.style.display = '';
      line.setAttribute('x1', xr(r.started_at).toFixed(1));
      line.setAttribute('x2', xr(end).toFixed(1));
    }
    for (const k of S.links) {
      const r = k.run;
      const shown = r.started_at != null && (head == null || r.started_at <= head);
      k.down.style.display = shown ? '' : 'none';
      if (shown) {
        const xs = xr(k.step.started_at), xc = xr(r.started_at);
        k.down.setAttribute('d', `M${xs.toFixed(1)} ${k.yb} V${k.yc} H${Math.max(xs, xc).toFixed(1)}`);
      }
      const childRunning = isRunning(r) && head == null;
      k.down.classList.toggle('is-live', childRunning);
      const ended = r.ended_at != null && (head == null || r.ended_at <= head);
      k.back.style.display = ended ? '' : 'none';
      if (ended) { const xe = xr(r.ended_at); k.back.setAttribute('d', `M${xe.toFixed(1)} ${k.yc} V${k.yb}`); }
    }
    // playhead
    if (head != null) {
      const x = xr(head);
      gHead.style.display = '';
      headLine.setAttribute('x1', x); headLine.setAttribute('x2', x); headLine.setAttribute('y1', 0); headLine.setAttribute('y2', S.totalH);
      const label = fmtOffset(head - origin);
      const t = headChip.querySelector('text'), rc = headChip.querySelector('rect');
      t.textContent = label;
      const wd = label.length * 6.6 + 12;
      const cx = clamp(x - wd / 2, 2, S.width - wd - 2);
      rc.setAttribute('width', wd); rc.setAttribute('x', cx); t.setAttribute('x', cx + 6);
    } else gHead.style.display = 'none';
    // lanes not started yet (replay)
    for (const [id, btn] of S.labelEls || []) {
      const r = S.laneTop.get(id).run;
      btn.classList.toggle('is-future', head != null && r.started_at > head);
    }
    S.dirty = false;
  }

  function frame(ts) {
    S.raf = 0;
    if (S.destroyed) return;
    const needLoop = S.live && S.playhead == null && !document.hidden;
    if (S.dirty || (needLoop && ts - S.last > 33)) { S.last = ts; render(); }
    if (needLoop || S.dirty) S.raf = requestAnimationFrame(frame);
  }
  function kick() { if (!S.raf && !S.destroyed) S.raf = requestAnimationFrame(frame); }

  // ---- interaction ------------------------------------------------------------
  const localX = (e) => e.clientX - svgEl.getBoundingClientRect().left;
  const localY = (e) => e.clientY - svgEl.getBoundingClientRect().top;

  function showTip(e, stepEl) {
    const s = S.stepById.get(stepEl.dataset.step);
    if (!s) return;
    const lane = S.laneTop.get(s.run_id);
    const dur = s.duration_ms != null ? s.duration_ms : (s.ended_at != null ? s.ended_at - s.started_at : Date.now() - s.started_at);
    tip.replaceChildren(
      h('div.tl__tip-title', s.title),
      h('div.tl__tip-meta', `${STEP_KIND_LABEL[s.kind] || s.kind}${lane ? ' · ' + lane.run.agent_name : ''}`),
      h('div.tl__tip-row', h('span', s.status === 'running' ? 'Идёт' : 'Длительность'), h('span.mono', fmtDuration(dur))),
      h('div.tl__tip-row', h('span', 'Время'), h('span.mono', `${fmtClock(s.started_at)} – ${s.ended_at != null ? fmtClock(s.ended_at) : '…'}`)));
    tip.hidden = false;
    const host = el.getBoundingClientRect();
    const w = tip.offsetWidth, hh = tip.offsetHeight;
    let left = e.clientX - host.left + 14, top = e.clientY - host.top + 16;
    if (left + w > host.width - 4) left = e.clientX - host.left - w - 14;
    if (top + hh > host.height - 4) top = e.clientY - host.top - hh - 12;
    tip.style.transform = `translate(${Math.max(4, Math.round(left))}px, ${Math.max(4, Math.round(top))}px)`;
  }

  svgEl.addEventListener('pointermove', (e) => {
    if (S.seeking) { onSeek && onSeek(clamp(tOf(localX(e)), S.rootStart, contentRange().end)); return; }
    if (S.drag) {
      const dx = e.clientX - S.drag.x;
      if (!S.drag.moved && Math.abs(dx) > 3) { S.drag.moved = true; svgEl.setPointerCapture(e.pointerId); svgEl.classList.add('is-drag'); tip.hidden = true; }
      if (S.drag.moved) { setMode('manual'); const ms = (S.drag.d1 - S.drag.d0) / plotW(); S.d0 = S.drag.d0 - dx * ms; S.d1 = S.drag.d1 - dx * ms; S.dirty = true; kick(); }
      return;
    }
    const t = e.target.closest && e.target.closest('[data-step]');
    if (t) showTip(e, t); else tip.hidden = true;
  });
  svgEl.addEventListener('pointerleave', () => { tip.hidden = true; });
  svgEl.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return;
    if (localY(e) < AXIS_H && onSeek && !S.live) { S.seeking = true; svgEl.setPointerCapture(e.pointerId); onSeek(clamp(tOf(localX(e)), S.rootStart, contentRange().end)); return; }
    S.drag = { x: e.clientX, d0: S.d0, d1: S.d1, moved: false, target: e.target };
  });
  const endPointer = (e) => {
    if (S.seeking) { S.seeking = false; return; }
    const d = S.drag; S.drag = null;
    svgEl.classList.remove('is-drag');
    if (!d || d.moved) return;
    const st = e.target.closest && e.target.closest('[data-step]');
    if (st) { const s = S.stepById.get(st.dataset.step); onSelectStep && onSelectStep(s.id, s.run_id); return; }
    const ln = e.target.closest && e.target.closest('[data-lane]');
    if (ln) onSelectLane && onSelectLane(ln.dataset.lane);
  };
  svgEl.addEventListener('pointerup', endPointer);
  svgEl.addEventListener('pointercancel', () => { S.drag = null; S.seeking = false; svgEl.classList.remove('is-drag'); });
  svgEl.addEventListener('wheel', (e) => {
    if (e.ctrlKey || e.metaKey) {
      e.preventDefault();
      zoomAt(Math.exp(e.deltaY * 0.0025), tOf(localX(e)));
    } else if (e.shiftKey || Math.abs(e.deltaX) > Math.abs(e.deltaY)) {
      e.preventDefault();
      panBy(((e.deltaX || e.deltaY) / plotW()) * (S.d1 - S.d0));
    }
  }, { passive: false });
  svgEl.addEventListener('keydown', (e) => {
    const span = S.d1 - S.d0;
    if (e.key === '+' || e.key === '=') zoomAt(1 / 1.5, centerTime());
    else if (e.key === '-') zoomAt(1.5, centerTime());
    else if (e.key === '0') fit();
    else if (e.key === 'ArrowLeft') panBy(-span * 0.15);
    else if (e.key === 'ArrowRight') panBy(span * 0.15);
    else return;
    e.preventDefault();
  });

  const ro = typeof ResizeObserver === 'function' ? new ResizeObserver(() => { S.width = svgEl.parentNode.clientWidth; S.dirty = true; kick(); }) : null;
  if (ro) ro.observe(svgEl.parentNode);
  const onVis = () => { S.dirty = true; kick(); };
  document.addEventListener('visibilitychange', onVis);

  return {
    el,
    setData({ runs, steps }) {
      S.runs = runs; S.steps = steps;
      S.live = runs.some(isRunning);
      layout();
      S.width = svgEl.parentNode.clientWidth || S.width;
      kick();
    },
    select({ stepId, runId } = {}) { S.selStep = stepId ?? null; S.selLane = runId ?? S.selLane; syncSelection(); },
    setPlayhead(t) { S.playhead = t; S.dirty = true; kick(); },
    range() { const { start, end } = contentRange(); return { start, end }; },
    fit,
    destroy() { S.destroyed = true; cancelAnimationFrame(S.raf); if (ro) ro.disconnect(); document.removeEventListener('visibilitychange', onVis); },
  };
}
