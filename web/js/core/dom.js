// Tiny element builder. Text is always inserted as text nodes: there is no innerHTML anywhere.
const SVG_NS = 'http://www.w3.org/2000/svg';
const PROP_KEYS = new Set(['value', 'checked', 'disabled', 'hidden', 'tabIndex', 'selected', 'indeterminate', 'readOnly', 'textContent']);

function parseTag(spec) {
  const m = /^([a-zA-Z][\w-]*)?((?:[.#][\w-]+)*)$/.exec(spec);
  if (!m) return { tag: spec, classes: [], id: null };
  const classes = [];
  let id = null;
  for (const part of (m[2] || '').match(/[.#][\w-]+/g) || []) {
    if (part[0] === '.') classes.push(part.slice(1)); else id = part.slice(1);
  }
  return { tag: m[1] || 'div', classes, id };
}

function classString(v) {
  if (Array.isArray(v)) return v.flat(Infinity).filter(Boolean).join(' ');
  if (v && typeof v === 'object') return Object.keys(v).filter((k) => v[k]).join(' ');
  return v || '';
}

function applyProps(el, props, isSvg, extraClasses) {
  const cls = [...extraClasses];
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === 'class' || k === 'className') cls.push(classString(v));
    else if (k === 'style') {
      if (typeof v === 'string') el.style.cssText = v;
      else for (const [p, val] of Object.entries(v)) {
        if (val == null) continue;
        if (p.startsWith('--')) el.style.setProperty(p, val); else el.style[p] = val;
      }
    } else if (k === 'on') {
      for (const [ev, fn] of Object.entries(v)) if (fn) el.addEventListener(ev, fn);
    } else if (k.length > 2 && k.startsWith('on') && typeof v === 'function') {
      el.addEventListener(k.slice(2).toLowerCase(), v);
    } else if (k === 'dataset') {
      for (const [dk, dv] of Object.entries(v)) if (dv != null) el.dataset[dk] = dv;
    } else if (k === 'ref' && typeof v === 'function') {
      v(el);
    } else if (!isSvg && PROP_KEYS.has(k)) {
      el[k] = v;
    } else {
      el.setAttribute(k, v === true ? '' : String(v));
    }
  }
  const joined = cls.filter(Boolean).join(' ');
  if (joined) el.setAttribute('class', joined);
}

export function append(parent, kids) {
  for (const k of kids) {
    if (k == null || k === false || k === true) continue;
    if (Array.isArray(k)) append(parent, k);
    else if (k instanceof Node) parent.appendChild(k);
    else parent.appendChild(document.createTextNode(String(k)));
  }
  return parent;
}

/** h('div.card#id', { class, style, on:{click}, dataset, 'aria-label': 'x' }, ...children) */
export function h(spec, props, ...children) {
  if (props instanceof Node || Array.isArray(props) || typeof props === 'string' || typeof props === 'number') {
    children.unshift(props);
    props = null;
  }
  const { tag, classes, id } = parseTag(spec);
  const el = document.createElement(tag);
  if (id) el.id = id;
  applyProps(el, props, false, classes);
  append(el, children);
  return el;
}

/** svg('path', { d: '...' }) ; svg('svg.icon', { viewBox: '0 0 24 24' }, children) */
export function svg(spec, props, ...children) {
  if (props instanceof Node || Array.isArray(props) || typeof props === 'string') {
    children.unshift(props);
    props = null;
  }
  const { tag, classes, id } = parseTag(spec);
  const el = document.createElementNS(SVG_NS, tag);
  if (id) el.id = id;
  applyProps(el, props, true, classes);
  append(el, children);
  return el;
}

export const frag = (...children) => append(document.createDocumentFragment(), children);
export const text = (s) => document.createTextNode(s == null ? '' : String(s));

/** Replace all children of el with the given nodes. */
export function replace(el, ...children) {
  el.replaceChildren();
  append(el, children);
  return el;
}
export const clear = (el) => { el.replaceChildren(); return el; };

/** Add a listener and get an unsubscribe function back. */
export function on(target, type, fn, opts) {
  target.addEventListener(type, fn, opts);
  return () => target.removeEventListener(type, fn, opts);
}

export const qs = (sel, root = document) => root.querySelector(sel);
export const qsa = (sel, root = document) => Array.from(root.querySelectorAll(sel));
export const cx = (...c) => classString(c);

let uidN = 0;
export const uid = (prefix = 'o') => `${prefix}-${++uidN}`;

/** Set text only when it changed (keeps selections and avoids layout work). */
export function setText(el, value) {
  const s = value == null ? '' : String(value);
  if (el.textContent !== s) el.textContent = s;
}

/** Toggle several data/aria/class states cheaply. */
export function toggleClass(el, name, on) { el.classList.toggle(name, !!on); }

export const SVG = SVG_NS;
