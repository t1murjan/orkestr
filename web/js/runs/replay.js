// Replay scrubber for a finished run: play / pause, 1x / 4x / 16x, a range slider. The playhead is reported in absolute ms.
import { h } from '../core/dom.js';
import { iconButton, segmented, icon } from '../ui/index.js';
import { fmtDuration } from '../core/format.js';
import { xicon, fmtOffset } from './util.js';

export function createReplay({ onChange, getRange }) {
  const S = { t: null, playing: false, speed: 4, raf: 0, last: 0, available: false };
  const playBtn = iconButton({ icon: 'play', label: 'Воспроизвести', variant: 'secondary', onClick: () => toggle() });
  const speed = segmented({ options: [{ value: '1', label: '1×' }, { value: '4', label: '4×' }, { value: '16', label: '16×' }], value: '4', size: 'sm', ariaLabel: 'Скорость повтора', onChange: (v) => { S.speed = Number(v); } });
  const range = h('input.rp__range', { type: 'range', min: 0, max: 1000, step: 1, value: 0, 'aria-label': 'Положение повтора' });
  const pos = h('span.rp__pos.mono', '0 с');
  const total = h('span.rp__total.mono');
  const exit = iconButton({ icon: 'x', label: 'Выйти из повтора', size: 'sm', onClick: () => stop(true) });
  const hint = h('span.rp__hint', 'Повтор доступен после завершения запуска');
  const el = h('div.rp', playBtn, speed, h('div.rp__track', range), h('div.rp__time', pos, h('span.muted', ' / '), total), exit, hint);
  exit.hidden = true;

  const span = () => { const r = getRange(); return Math.max(1, r.end - r.start); };
  function paint() {
    const sp = span();
    const rel = S.t == null ? 0 : S.t - getRange().start;
    range.max = String(Math.round(sp));
    range.value = String(Math.round(rel));
    range.style.setProperty('--p', (S.t == null ? 0 : (rel / sp) * 100).toFixed(2) + '%');
    pos.textContent = S.t == null ? '0 с' : fmtOffset(rel);
    total.textContent = fmtDuration(sp);
    playBtn.update({ icon: S.playing ? 'pause' : 'play', label: S.playing ? 'Пауза' : 'Воспроизвести' });
    exit.hidden = S.t == null;
    el.classList.toggle('is-on', S.t != null);
  }
  // pause icon is not in the shared set: swap the glyph by hand
  const origUpdate = playBtn.update.bind(playBtn);
  playBtn.update = (p) => {
    if (p.icon === 'pause') { origUpdate({ ...p, icon: 'play' }); const svgEl = playBtn.querySelector('svg'); if (svgEl) svgEl.replaceWith(xicon('pause')); }
    else origUpdate(p);
  };

  function emit() { paint(); onChange(S.t); }
  function tick(ts) {
    S.raf = 0;
    if (!S.playing) return;
    const dt = Math.min(100, ts - S.last); S.last = ts;
    const r = getRange();
    S.t = Math.min(r.end, (S.t == null ? r.start : S.t) + dt * S.speed);
    if (S.t >= r.end) { S.playing = false; }
    emit();
    if (S.playing) S.raf = requestAnimationFrame(tick);
  }
  function play() {
    const r = getRange();
    if (S.t == null || S.t >= r.end - 1) S.t = r.start;
    S.playing = true; S.last = performance.now();
    emit();
    if (!S.raf) S.raf = requestAnimationFrame(tick);
  }
  function pause() { S.playing = false; cancelAnimationFrame(S.raf); S.raf = 0; paint(); }
  function toggle() { if (!S.available) return; if (S.playing) pause(); else play(); }
  function stop(clear) { pause(); if (clear) { S.t = null; emit(); } }

  range.addEventListener('input', () => {
    if (!S.available) return;
    pause();
    S.t = getRange().start + Number(range.value);
    emit();
  });
  paint();

  return {
    el,
    get t() { return S.t; },
    get active() { return S.t != null; },
    seek(t) { if (!S.available) return; pause(); S.t = t; emit(); },
    setAvailable(on) {
      S.available = on;
      playBtn.update({ disabled: !on });
      range.disabled = !on;
      hint.hidden = on;
      el.classList.toggle('is-off', !on);
      if (!on && S.t != null) stop(true);
      paint();
    },
    refresh: paint,
    destroy() { cancelAnimationFrame(S.raf); },
  };
}
