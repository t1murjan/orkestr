import { h, svg } from '../core/dom.js';
import { RUNTIME_LABEL } from '../core/format.js';

/** Quiet text badge: badge({ text: 'демо', tone: 'neutral'|'ok'|'warn'|'err'|'accent' }) */
export function badge({ text, tone = 'neutral', title } = {}) {
  const el = h('span', { class: `badge badge--${tone}`, title }, text);
  el.update = (p) => { if (p.text != null) el.textContent = p.text; };
  return el;
}

/**
 * Status dot: statusDot({ status: 'idle'|'working'|'delegating'|'error'|'ok', size=8, label })
 * working and delegating pulse softly (opacity, 2 s) because they are live; nothing else moves.
 */
export function statusDot({ status = 'idle', size = 8, label } = {}) {
  const el = h('span', { class: 'dot', role: label ? 'img' : null, 'aria-label': label || null, 'aria-hidden': label ? null : 'true' });
  el.style.setProperty('--dot', size + 'px');
  el.dataset.status = status;
  el.update = ({ status: s, label: l }) => {
    if (s) el.dataset.status = s;
    if (l != null) { el.setAttribute('aria-label', l); el.setAttribute('role', 'img'); el.removeAttribute('aria-hidden'); }
  };
  return el;
}

/** keycap('⌘K') -> <kbd class="keycap"> */
export function keycap(text) { return h('kbd.keycap', text); }

const MARK_COLOR = { claude_code: 'var(--rt-claude)', opencode: 'var(--rt-opencode)', demo: 'var(--rt-demo)' };

/**
 * Runtime identity mark, a flat glyph in the runtime colour: runtimeMark({ runtime, size=16, label=false })
 * Claude Code = asterisk, OpenCode = bracketed square, demo = dashed ring. Shape differs, not only colour.
 */
export function runtimeMark({ runtime, size = 16, label = false } = {}) {
  const color = MARK_COLOR[runtime] || 'var(--rt-demo)';
  const stroke = { stroke: color, 'stroke-width': 1.75, 'stroke-linecap': 'round', fill: 'none' };
  let glyph;
  if (runtime === 'claude_code') {
    glyph = [
      svg('path', { ...stroke, d: 'M8 2.5v11' }),
      svg('path', { ...stroke, d: 'M3.25 5.25l9.5 5.5' }),
      svg('path', { ...stroke, d: 'M12.75 5.25l-9.5 5.5' }),
    ];
  } else if (runtime === 'opencode') {
    glyph = [
      svg('rect', { ...stroke, x: 2.5, y: 2.5, width: 11, height: 11, rx: 2.5 }),
      svg('rect', { x: 6, y: 6, width: 4, height: 4, rx: 1, fill: color }),
    ];
  } else {
    glyph = [svg('circle', { ...stroke, cx: 8, cy: 8, r: 5.25, 'stroke-dasharray': '2.2 2.4' })];
  }
  const mark = svg('svg', { class: 'rt-mark', width: size, height: size, viewBox: '0 0 16 16', 'aria-hidden': 'true' }, glyph);
  if (!label) {
    mark.setAttribute('role', 'img');
    mark.removeAttribute('aria-hidden');
    mark.setAttribute('aria-label', RUNTIME_LABEL[runtime] || String(runtime || ''));
    return mark;
  }
  return h('span.rt-label', mark, h('span', RUNTIME_LABEL[runtime] || String(runtime || '')));
}

/** The product mark for the wordmark: a hierarchy of three nodes, flat, one colour. */
export function brandMark(size = 20) {
  return svg('svg', { class: 'brand-mark', width: size, height: size, viewBox: '0 0 20 20', 'aria-hidden': 'true', fill: 'none', stroke: 'currentColor', 'stroke-width': 1.5, 'stroke-linecap': 'round' }, [
    svg('path', { d: 'M10 6.5v3.25M10 9.75H4.75V13M10 9.75h5.25V13' }),
    svg('rect', { x: 7.5, y: 1.75, width: 5, height: 4.75, rx: 1.5, fill: 'currentColor', stroke: 'none' }),
    svg('rect', { x: 2.25, y: 13, width: 5, height: 4.75, rx: 1.5 }),
    svg('rect', { x: 12.75, y: 13, width: 5, height: 4.75, rx: 1.5 }),
  ]);
}
