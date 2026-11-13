// Floating layers: popover, hover card, tooltip, drawer, modal, confirm, toast.
import { h, on } from '../core/dom.js';
import { popIn, popOut, reducedMotion, EASE_CSS } from '../core/motion.js';
import { iconButton, button } from './button.js';
import { statusDot } from './badge.js';

export function overlayRoot() {
  let r = document.getElementById('overlay-root');
  if (!r) { r = h('div#overlay-root'); document.body.appendChild(r); }
  return r;
}

const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
export const focusables = (root) => [...root.querySelectorAll(FOCUSABLE)].filter((el) => el.offsetParent !== null || el === document.activeElement);

/** Keep Tab inside container. Returns release(). */
export function trapFocus(container) {
  const handler = (e) => {
    if (e.key !== 'Tab') return;
    const list = focusables(container);
    if (!list.length) { e.preventDefault(); container.focus(); return; }
    const first = list[0], last = list[list.length - 1];
    if (e.shiftKey && (document.activeElement === first || document.activeElement === container)) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  };
  container.addEventListener('keydown', handler);
  return () => container.removeEventListener('keydown', handler);
}

/**
 * Position `el` (position: fixed, already in the DOM) next to `anchor`.
 * placement: top|bottom|left|right with optional -start|-end. Flips when it does not fit. Returns the placement used.
 */
export function place(el, anchor, placement = 'bottom-start', offset = 6) {
  const a = anchor instanceof Element ? anchor.getBoundingClientRect() : anchor;
  const w = el.offsetWidth, hgt = el.offsetHeight;
  const vw = document.documentElement.clientWidth, vh = document.documentElement.clientHeight;
  const m = 8;
  let [side, align = 'center'] = placement.split('-');
  const fits = {
    top: a.top - offset - hgt >= m, bottom: a.bottom + offset + hgt <= vh - m,
    left: a.left - offset - w >= m, right: a.right + offset + w <= vw - m,
  };
  const opposite = { top: 'bottom', bottom: 'top', left: 'right', right: 'left' };
  if (!fits[side] && fits[opposite[side]]) side = opposite[side];
  let x, y;
  if (side === 'top' || side === 'bottom') {
    y = side === 'top' ? a.top - offset - hgt : a.bottom + offset;
    x = align === 'start' ? a.left : align === 'end' ? a.right - w : a.left + a.width / 2 - w / 2;
  } else {
    x = side === 'left' ? a.left - offset - w : a.right + offset;
    y = align === 'start' ? a.top : align === 'end' ? a.bottom - hgt : a.top + a.height / 2 - hgt / 2;
  }
  x = Math.max(m, Math.min(x, vw - w - m));
  y = Math.max(m, Math.min(y, vh - hgt - m));
  el.style.left = Math.round(x) + 'px';
  el.style.top = Math.round(y) + 'px';
  el.dataset.side = side;
  return side;
}
const DIR_FOR_SIDE = { top: 'up', bottom: 'down', left: 'left', right: 'right' };

const openPopovers = new Set();

/**
 * popover({ anchor, content: Node, placement='bottom-start', offset=6, matchWidth=false, className, onClose, closeOnOutside=true })
 * -> { el, close(), reposition(), isOpen } ; closes on Escape and outside click.
 */
export function popover({ anchor, content, placement = 'bottom-start', offset = 6, matchWidth = false, className = '', onClose, closeOnOutside = true, role = 'dialog' } = {}) {
  const el = h('div', { class: ['popover', className], role });
  el.append(content);
  overlayRoot().appendChild(el);
  if (matchWidth && anchor) el.style.minWidth = anchor.getBoundingClientRect().width + 'px';
  let open = true;
  const reposition = () => { if (open && anchor) place(el, anchor, placement, offset); };
  reposition();
  popIn(el, { dir: DIR_FOR_SIDE[el.dataset.side] || 'down', distance: 6 });
  const offs = [
    on(document, 'pointerdown', (e) => { if (closeOnOutside && !el.contains(e.target) && !(anchor && anchor.contains(e.target))) api.close(); }, true),
    on(document, 'keydown', (e) => { if (e.key === 'Escape' && openPopovers.has(api) && [...openPopovers].pop() === api) { e.stopPropagation(); api.close(true); } }, true),
    on(window, 'resize', reposition),
    on(window, 'scroll', reposition, true),
  ];
  const api = {
    el, reposition,
    get isOpen() { return open; },
    close(restore = false) {
      if (!open) return;
      open = false;
      openPopovers.delete(api);
      offs.forEach((f) => f());
      popOut(el).then(() => el.remove());
      if (restore && anchor && anchor.focus) anchor.focus();
      if (onClose) onClose();
    },
  };
  openPopovers.add(api);
  return api;
}

// ---- hover card -------------------------------------------------------------
let activeHover = null;

/**
 * Hover card that follows an anchor and stays open while the pointer is inside it.
 * hoverCard(anchor, content: Node | (() => Node), { placement='right-start', delay=250, closeDelay=160, offset=8, width })
 * -> { open(), close(), destroy() }. The content node may define destroy() for cleanup.
 */
export function hoverCard(anchor, content, { placement = 'right-start', delay = 250, closeDelay = 160, offset = 8, width, className = '' } = {}) {
  let el = null, body = null, openT = 0, closeT = 0, offs = [], pt = null, target = anchor;
  const build = () => (typeof content === 'function' ? content() : content);
  const open = () => {
    clearTimeout(closeT);
    if (el) return;
    if (activeHover && activeHover !== api) activeHover.close();
    body = build();
    if (!body) return;
    el = h('div', { class: ['popover', 'hovercard', className], role: 'tooltip' }, body);
    if (width) el.style.width = width + 'px';
    overlayRoot().appendChild(el);
    // wide anchors (table rows, list rows): hang the card off the pointer instead of the far edge
    const r = anchor.getBoundingClientRect();
    target = r.width > 360 && pt ? { left: pt.x, right: pt.x, top: r.top, bottom: r.bottom, width: 0, height: r.height } : anchor;
    place(el, target, placement, offset);
    popIn(el, { dir: DIR_FOR_SIDE[el.dataset.side] || 'right', distance: 6 });
    el.addEventListener('pointerenter', () => clearTimeout(closeT));
    el.addEventListener('pointerleave', scheduleClose);
    offs = [
      on(window, 'resize', () => el && place(el, target, placement, offset)),
      on(document, 'keydown', (e) => { if (e.key === 'Escape') close(); }),
      on(window, 'scroll', () => close(), true),
    ];
    activeHover = api;
  };
  const close = () => {
    clearTimeout(openT); clearTimeout(closeT);
    if (!el) return;
    const node = el, b = body;
    el = null; body = null;
    offs.forEach((f) => f()); offs = [];
    if (b && typeof b.destroy === 'function') b.destroy();
    popOut(node, { duration: 120 }).then(() => node.remove());
    if (activeHover === api) activeHover = null;
  };
  const scheduleOpen = () => { clearTimeout(closeT); clearTimeout(openT); openT = setTimeout(open, delay); };
  const scheduleClose = () => { clearTimeout(openT); clearTimeout(closeT); closeT = setTimeout(close, closeDelay); };
  const subs = [
    on(anchor, 'pointerenter', (e) => { if (e.pointerType !== 'touch') scheduleOpen(); }),
    on(anchor, 'pointermove', (e) => { if (!el) pt = { x: e.clientX, y: e.clientY }; }),
    on(anchor, 'pointerleave', scheduleClose),
    on(anchor, 'focusin', () => { clearTimeout(closeT); openT = setTimeout(open, 100); }),
    on(anchor, 'focusout', scheduleClose),
  ];
  const api = { open, close, destroy() { close(); subs.forEach((f) => f()); } };
  return api;
}

/** Text tooltip: tooltip(anchor, 'Копировать', { placement='top', delay=450 }) -> destroy() */
export function tooltip(anchor, label, { placement = 'top', delay = 450 } = {}) {
  let el = null, t = 0;
  const show = () => {
    if (el) return;
    const txt = typeof label === 'function' ? label() : label;
    if (!txt) return;
    el = h('div.tooltip', { role: 'tooltip' }, txt);
    overlayRoot().appendChild(el);
    place(el, anchor, placement, 6);
    popIn(el, { dir: DIR_FOR_SIDE[el.dataset.side] || 'up', distance: 4, duration: 150 });
  };
  const hide = () => { clearTimeout(t); if (el) { const n = el; el = null; n.remove(); } };
  const schedule = () => { clearTimeout(t); t = setTimeout(show, delay); };
  const offs = [on(anchor, 'pointerenter', schedule), on(anchor, 'pointerleave', hide), on(anchor, 'focusin', schedule), on(anchor, 'focusout', hide), on(anchor, 'pointerdown', hide)];
  return () => { hide(); offs.forEach((f) => f()); };
}

// ---- modal and drawer ---------------------------------------------------------
const layers = [];

function makeLayer({ kind, title, content, footer, width, onClose, dismissable = true, description }) {
  const prevFocus = document.activeElement;
  const titleId = 'dlg-' + Math.random().toString(36).slice(2, 8);
  const closeBtn = iconButton({ icon: 'x', label: 'Закрыть', size: 'sm', onClick: () => api.close() });
  const dialog = h('div', { class: `${kind}__panel`, role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': titleId, tabindex: -1 },
    h('header', { class: `${kind}__head` }, h('h2', { class: `${kind}__title`, id: titleId }, title), dismissable ? closeBtn : null),
    description ? h('p', { class: `${kind}__desc` }, description) : null,
    h('div', { class: `${kind}__body` }, content),
    footer && footer.length ? h('footer', { class: `${kind}__foot` }, footer) : null);
  if (width) dialog.style.width = width + 'px';
  const scrim = h('div', { class: `${kind}`, dataset: { kind } }, dialog);
  scrim.addEventListener('pointerdown', (e) => { if (dismissable && e.target === scrim) api.close(); });
  overlayRoot().appendChild(scrim);
  const release = trapFocus(dialog);
  let closed = false, resolveClosed;
  const closedPromise = new Promise((r) => { resolveClosed = r; });
  const keyOff = on(document, 'keydown', (e) => {
    if (e.key === 'Escape' && dismissable && layers[layers.length - 1] === api) { e.stopPropagation(); api.close(); }
  });
  // enter
  if (!reducedMotion()) {
    scrim.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 200, easing: EASE_CSS });
    dialog.animate(kind === 'drawer'
      ? [{ opacity: 0, transform: 'translateX(24px)' }, { opacity: 1, transform: 'none' }]
      : [{ opacity: 0, transform: 'translateY(8px)' }, { opacity: 1, transform: 'none' }],
    { duration: kind === 'drawer' ? 360 : 200, easing: EASE_CSS });
  }
  const api = {
    el: scrim, dialog, closed: closedPromise,
    setContent(...nodes) { dialog.querySelector(`.${kind}__body`).replaceChildren(...nodes); },
    setTitle(t) { dialog.querySelector(`.${kind}__title`).textContent = t; },
    close(result) {
      if (closed) return;
      closed = true;
      layers.splice(layers.indexOf(api), 1);
      keyOff(); release();
      Promise.all([popOut(scrim, { duration: 150 })]).then(() => scrim.remove());
      if (prevFocus && prevFocus.focus && document.contains(prevFocus)) prevFocus.focus();
      if (onClose) onClose(result);
      resolveClosed(result);
    },
  };
  layers.push(api);
  queueMicrotask(() => {
    const target = dialog.querySelector('[data-autofocus]') || focusables(dialog.querySelector(`.${kind}__body`))[0] || (dismissable ? closeBtn : null) || dialog;
    target.focus();
  });
  return api;
}

/** modal({ title, content, footer: Node[], width=480, onClose, dismissable=true, description }) -> { el, close(result), closed: Promise, setContent, setTitle } */
export const modal = (o) => makeLayer({ ...o, kind: 'modal', width: o.width || 480 });
/** drawer({ title, content, footer, width=480, onClose }) -> same handle; slides from the right */
export const drawer = (o) => makeLayer({ ...o, kind: 'drawer', width: o.width || 480 });

/** confirm({ title, message, confirmLabel, cancelLabel, danger }) -> Promise<boolean> */
export function confirm({ title = 'Подтвердите действие', message, confirmLabel = 'Подтвердить', cancelLabel = 'Отмена', danger = false } = {}) {
  return new Promise((resolve) => {
    const ok = button({ label: confirmLabel, variant: danger ? 'danger' : 'primary', onClick: () => m.close(true) });
    ok.dataset.autofocus = '';
    const m = modal({
      title, width: 420,
      content: message ? h('p.modal__text', message) : null,
      footer: [button({ label: cancelLabel, variant: 'quiet', onClick: () => m.close(false) }), ok],
      onClose: (r) => resolve(r === true),
    });
  });
}

// ---- toast -----------------------------------------------------------------
function toastRoot() {
  let r = document.getElementById('toast-root');
  if (!r) { r = h('div#toast-root', { role: 'region', 'aria-label': 'Уведомления', 'aria-live': 'polite' }); document.body.appendChild(r); }
  return r;
}
/** toast('Агент создан', { kind: 'info'|'ok'|'error', timeout=4000, action: { label, onClick } }) -> { close() } */
export function toast(message, { kind = 'info', timeout = 4500, action } = {}) {
  const el = h('div', { class: `toast toast--${kind}`, role: kind === 'error' ? 'alert' : 'status' },
    kind === 'info' ? null : statusDot({ status: kind === 'ok' ? 'ok' : 'error' }),
    h('span.toast__text', message),
    action ? button({ label: action.label, variant: 'quiet', size: 'sm', onClick: () => { action.onClick(); close(); } }) : null,
    iconButton({ icon: 'x', label: 'Закрыть', size: 'sm', onClick: () => close() }));
  toastRoot().appendChild(el);
  popIn(el, { dir: 'up', distance: 8, duration: 200 });
  let t = 0, done = false;
  const close = () => { if (done) return; done = true; clearTimeout(t); popOut(el).then(() => el.remove()); };
  if (timeout) t = setTimeout(close, timeout);
  return { close };
}
