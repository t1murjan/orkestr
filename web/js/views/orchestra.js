// Оркестр: the canvas of agents. Arrange them into a hierarchy, direct them, watch delegations travel down the links.
import { h, svg, replace } from '../core/dom.js';
import { fmtPercent } from '../core/format.js';
import { reducedMotion, tween } from '../core/motion.js';
import { live } from '../core/live.js';
import { button, iconButton, toast, confirm, keycap, icon } from '../ui/index.js';
import { NODE_W, NODE_H, R, boundsOf, snap, layeredLayout, LAYER_GAP } from '../orchestra/geometry.js';
import { createViewport } from '../orchestra/viewport.js';
import { createNode } from '../orchestra/node.js';
import { createLinkLayer } from '../orchestra/links.js';
import { createDelegationTracker } from '../orchestra/delegations.js';
import { createDrawer } from '../orchestra/drawer.js';
import { createComposer } from '../orchestra/composer.js';
import { createSessionsLane } from '../orchestra/sessions.js';
import { noAgents, connecting } from '../orchestra/empty.js';

const REASON = { self_edge: 'Это он сам', duplicate_edge: 'Уже подопечный', cycle: 'Замкнёт цикл' };
const REASON_LONG = {
  self_edge: 'Агент не может быть подопечным самому себе',
  duplicate_edge: 'Эта связь уже есть',
  cycle: 'Связь замкнёт цикл: руководитель окажется подопечным своего подопечного',
};
const DRAWER_W = 400;

let M = null; // the mounted screen, released in unmount

export default {
  id: 'orchestra',
  title: 'Оркестр',
  layout: 'canvas',
  mount(root, ctx) {
    const { store, api, router } = ctx;
    const offs = [];
    const timers = new Set();
    let dead = false;
    const S = {
      sel: null,            // { type: 'agent' | 'edge', id }
      mode: null,           // { kind: 'click' | 'drag', sup }
      pos: new Map(),       // local positions while dragging / saving
      dirty: new Map(),     // agent id -> debounce timer for PATCH {x, y}
      nodes: new Map(),
      hover: null,
      lingering: new Map(), // key -> {sup, sub} ghost links that stay until their return dash is done
      fitted: false,
      emptyKind: null,
      raf: 0,
    };

    // ---- DOM -----------------------------------------------------------------------------------
    const linksSvg = svg('svg', { class: 'orch-links', width: 1, height: 1, 'aria-hidden': 'false' });
    const nodesLayer = h('div.orch-nodes');
    const world = h('div.orch-world', linksSvg, nodesLayer);
    const viewportEl = h('div.orch-viewport', world);
    const announce = h('div.visually-hidden', { role: 'status', 'aria-live': 'polite' });
    const barHost = h('div.orch-bar', { dataset: { nopan: '' } });
    const zoomLabel = h('span.orch-zoom__v.mono', '100 %');
    const zoomEl = h('div.orch-zoom', { dataset: { nopan: '' }, role: 'group', 'aria-label': 'Масштаб' },
      iconButton({ icon: 'minus', label: 'Уменьшить', size: 'sm', onClick: () => vp.zoomBy(1 / 1.25) }),
      zoomLabel,
      iconButton({ icon: 'plus', label: 'Увеличить', size: 'sm', onClick: () => vp.zoomBy(1.25) }),
      button({ label: 'Вписать', variant: 'quiet', size: 'sm', kbd: 'F', title: 'Вписать всё на экран (F)', onClick: () => fit(true) }));
    const emptyHost = h('div.orch-emptyhost');

    const vp = createViewport(viewportEl, world, {
      onView: (v) => { zoomLabel.textContent = fmtPercent(Math.round(v.k * 100)); },
      onPanStart: () => closeHovers(),
      onBackgroundClick: () => { if (S.mode) cancelMode(); else if (S.sel) { setSel(null); drawer.close(); } },
      onBackgroundDblClick: (pt) => { if (!store.get('loaded') || S.mode) return; createAgent({ x: snap(pt.x - NODE_W / 2), y: snap(pt.y - R) }); },
    });
    const layer = createLinkLayer(linksSvg, {
      onSelect: (edgeId) => { if (S.mode) return; setSel({ type: 'edge', id: edgeId }); drawer.close(); },
      onDelete: (edgeId) => removeEdge(store.get('edges').find((e) => e.id === edgeId)),
    });

    const drawer = createDrawer({
      store, api, router,
      onClose: () => relayout(),
      onDelete: (a) => deleteAgent(a),
      onSelectAgent: (id) => { setSel({ type: 'agent', id }); drawer.open(id); ensureVisible(id); },
      onAssign: (id) => startAssign(id),
      onRemoveEdge: (e) => removeEdge(e),
    });
    const composer = createComposer({
      store, api, router,
      getSelectedId: () => (S.sel && S.sel.type === 'agent' ? S.sel.id : null),
      onPick: (id) => { setSel({ type: 'agent', id }); if (drawer.isOpen) drawer.open(id); },
    });
    const lane = createSessionsLane({ store, router });
    const stage = h('div.orch-stage', viewportEl, barHost, zoomEl, emptyHost, composer.el, drawer.el, announce);
    root.append(h('div.orch', lane.el, stage));

    // ---- helpers ---------------------------------------------------------------------------------
    const later = (fn, ms) => { const t = setTimeout(() => { timers.delete(t); if (!dead) fn(); }, ms); timers.add(t); return t; };
    const agentOf = (id) => store.agentById(id);
    const posOf = (id) => {
      const p = S.pos.get(id);
      if (p) return p;
      const a = agentOf(id);
      return a ? { x: a.x || 0, y: a.y || 0 } : null;
    };
    const closeHovers = () => { for (const n of S.nodes.values()) n.hover.close(); };
    const say = (t) => { announce.textContent = ''; later(() => { announce.textContent = t; }, 30); };
    const selectedAgent = () => (S.sel && S.sel.type === 'agent' ? agentOf(S.sel.id) : null);
    const relayout = () => { stage.classList.toggle('has-drawer', drawer.isOpen); };

    function fit(animate) {
      const list = store.get('agents').map((a) => posOf(a.id)).filter(Boolean);
      const box = boundsOf(list);
      const right = drawer.isOpen ? DRAWER_W : 0;
      vp.fitTo(box, { pad: 40, top: 48, bottom: composer.el.offsetHeight + 24, right, animate: animate && !reducedMotion() });
    }

    function ensureVisible(id) {
      const p = posOf(id);
      if (!p) return;
      const v = vp.view;
      const r = viewportEl.getBoundingClientRect();
      const right = r.width - (drawer.isOpen ? DRAWER_W : 0) - 16;
      const bottom = r.height - composer.el.offsetHeight - 32;
      const x0 = p.x * v.k + v.x, x1 = (p.x + NODE_W) * v.k + v.x;
      const y0 = p.y * v.k + v.y, y1 = (p.y + NODE_H) * v.k + v.y;
      let dx = 0, dy = 0;
      if (x1 > right) dx = right - x1;
      if (x0 + dx < 16) dx = 16 - x0;
      if (y1 > bottom) dy = bottom - y1;
      if (y0 + dy < 56) dy = 56 - y0;
      if (dx || dy) vp.animateTo({ x: v.x + dx, y: v.y + dy, k: v.k }, 360);
    }

    // ---- selection and modes ---------------------------------------------------------------------
    function setSel(sel) {
      S.sel = sel;
      renderStates();
      renderBar();
      composer.sync();
      renderGraph();
    }

    // Obsidian-style focus: the hovered or selected node, its links and direct neighbours stay lit, the rest steps back
    function focusSet() {
      if (S.mode) return null;
      const id = S.hover && agentOf(S.hover) ? S.hover : S.sel && S.sel.type === 'agent' ? S.sel.id : null;
      const edges = store.get('edges');
      if (id) {
        const ids = new Set([id]);
        for (const e of edges) { if (e.supervisor_id === id) ids.add(e.subordinate_id); if (e.subordinate_id === id) ids.add(e.supervisor_id); }
        return { id, ids };
      }
      if (S.sel && S.sel.type === 'edge') {
        const e = edges.find((x) => x.id === S.sel.id);
        if (e) return { id: null, ids: new Set([e.supervisor_id, e.subordinate_id]) };
      }
      return null;
    }
    function applyFocus() { renderStates(); renderGraph(); }

    function modeFor(id) {
      if (!S.mode) return {};
      if (id === S.mode.sup) return { source: true };
      const p = store.linkProblem(S.mode.sup, id);
      return p ? { mode: 'invalid', reason: REASON[p] } : { mode: 'target' };
    }
    function renderStates() {
      const f = focusSet();
      for (const [id, n] of S.nodes) n.setState({ selected: !!(S.sel && S.sel.type === 'agent' && S.sel.id === id), ...modeFor(id), hot: !!f && f.ids.has(id), dim: !!f && !f.ids.has(id) });
      stage.classList.toggle('is-linking', !!S.mode);
    }

    function startAssign(id) {
      if (!agentOf(id)) return;
      closeHovers();
      S.mode = { kind: 'click', sup: id };
      renderStates(); renderBar();
      say('Выберите подопечного: щёлкните по агенту. Escape отменяет');
    }
    function cancelMode() {
      if (!S.mode) return;
      S.mode = null;
      layer.setDraft(null);
      for (const n of S.nodes.values()) n.el.classList.remove('is-over');
      renderStates(); renderBar();
    }

    async function createEdge(sup, sub) {
      try {
        await api.post('/edges', { supervisor_id: sup, subordinate_id: sub });
        const a = agentOf(sup), b = agentOf(sub);
        if (a && b) say(`Связь создана: ${a.name}, подопечный ${b.name}`);
      } catch (e) {
        toast(e.message || 'Не удалось создать связь', { kind: 'error' });
      }
    }
    function tryLink(sup, sub) {
      const problem = store.linkProblem(sup, sub);
      if (problem) { toast(REASON_LONG[problem], { kind: 'error' }); return false; }
      createEdge(sup, sub);
      return true;
    }
    async function removeEdge(edge) {
      if (!edge) return;
      try {
        await api.del(`/edges/${edge.id}`);
        if (S.sel && S.sel.type === 'edge' && S.sel.id === edge.id) setSel(null);
        const a = agentOf(edge.supervisor_id), b = agentOf(edge.subordinate_id);
        if (a && b) say(`Связь удалена: ${a.name}, ${b.name}`);
      } catch (e) { toast(e.message || 'Не удалось удалить связь', { kind: 'error' }); }
    }

    async function deleteAgent(a) {
      const ok = await confirm({
        title: `Удалить агента «${a.name}»?`,
        message: 'Связи агента будут удалены, его активные запуски остановлены. Это нельзя отменить.',
        confirmLabel: 'Удалить агента', danger: true,
      });
      if (!ok) return;
      try {
        await api.del(`/agents/${a.id}`);
        if (S.sel && S.sel.id === a.id) setSel(null);
        if (drawer.agentId === a.id) drawer.close();
      } catch (e) { toast(e.message || 'Не удалось удалить агента', { kind: 'error' }); }
    }

    // ---- creating agents ---------------------------------------------------------------------------
    // a new agent goes next to what is already there: under the selected agent, else on a new row below the graph
    function freeSpot() {
      const list = store.get('agents').map((x) => posOf(x.id)).filter(Boolean);
      const taken = (x, y) => list.some((p) => Math.abs(p.x - x) < NODE_W + 16 && Math.abs(p.y - y) < NODE_H - 20);
      let x, y;
      const sel = selectedAgent();
      if (sel && posOf(sel.id)) { const p = posOf(sel.id); x = p.x; y = p.y + LAYER_GAP; }
      else if (list.length) { const bx = boundsOf(list); x = snap(bx.minx); y = snap(bx.maxy + 72); }
      else {
        const r = viewportEl.getBoundingClientRect();
        const c = vp.toWorld(r.left + r.width / 2, r.top + r.height / 2 - 60);
        x = snap(c.x - NODE_W / 2); y = snap(c.y - R);
      }
      for (let i = 0; i < 60 && taken(x, y); i++) x += NODE_W + 24;
      return { x: snap(x), y: snap(y) };
    }
    function whenInStore(id) {
      return new Promise((resolve) => {
        if (agentOf(id)) return resolve(true);
        let done = false;
        const finish = (v) => { if (done) return; done = true; off(); resolve(v); };
        const off = store.subscribe('agents', () => { if (agentOf(id)) finish(true); });
        later(() => { if (!done) store.reload().then(() => finish(!!agentOf(id))); }, 1200);
      });
    }
    async function createAgent(at) {
      const pos = at || freeSpot();
      const rts = store.get('runtimes');
      const runtime = ['claude_code', 'opencode', 'demo'].find((id) => rts.some((r) => r.id === id && r.available)) || 'demo';
      const names = new Set(store.get('agents').map((a) => a.name));
      let name = 'Новый агент', n = 1;
      while (names.has(name)) { n += 1; name = `Новый агент ${n}`; }
      try {
        const a = await api.post('/agents', { name, runtime, permissions: 'edit', role: '', x: pos.x, y: pos.y });
        if (!(await whenInStore(a.id))) return;
        setSel({ type: 'agent', id: a.id });
        drawer.open(a.id, { focusName: true });
        relayout();
        ensureVisible(a.id);
      } catch (e) { toast(e.message || 'Не удалось создать агента', { kind: 'error' }); }
    }

    // ---- automatic layout: layered, animated once, saved ---------------------------------------------------
    let layoutAnim = null;
    function autoLayout() {
      const agents = store.get('agents');
      if (!agents.length) return;
      if (layoutAnim) layoutAnim.cancel();
      const items = agents.map((a) => ({ id: a.id, x: posOf(a.id).x, y: posOf(a.id).y }));
      const target = layeredLayout(items, store.get('edges'));
      const from = new Map(items.map((i) => [i.id, { x: i.x, y: i.y }]));
      closeHovers();
      layoutAnim = tween({
        duration: 360,
        onUpdate: (e) => {
          for (const [id, t] of target) { const f = from.get(id); moveNodeNow(id, { x: f.x + (t.x - f.x) * e, y: f.y + (t.y - f.y) * e }); }
          renderGraph();
        },
        onDone: () => {
          layoutAnim = null;
          for (const id of target.keys()) { S.pos.set(id, target.get(id)); savePos(id); }
          fit(true);
          say('Граф упорядочен');
        },
      });
    }
    function moveNodeNow(id, p) { S.pos.set(id, p); const n = S.nodes.get(id); if (n) n.setPos(p); }

    // ---- positions: drag, nudge, debounced save -----------------------------------------------------
    function savePos(id) {
      clearTimeout(S.dirty.get(id));
      S.dirty.set(id, later(() => flushPos(id), 400));
    }
    function flushPos(id) {
      clearTimeout(S.dirty.get(id));
      S.dirty.delete(id);
      const p = S.pos.get(id);
      if (!p || !agentOf(id)) { S.pos.delete(id); return; }
      api.patch(`/agents/${id}`, { x: p.x, y: p.y })
        .catch((e) => { toast(e.message || 'Не удалось сохранить положение', { kind: 'error' }); S.pos.delete(id); scheduleRender(); })
        .then(() => { later(() => { if (!S.dirty.has(id)) { S.pos.delete(id); scheduleRender(); } }, 1500); });
    }
    function moveNode(id, p) {
      S.pos.set(id, p);
      const n = S.nodes.get(id);
      if (n) n.setPos(p);
      scheduleGraph();
    }

    let drag = null;
    const onNodeDown = (e) => {
      if (e.button !== 0 || S.mode) return;
      const wrap = e.target.closest('.onode-wrap');
      if (!wrap || e.target.closest('.onode__port')) return;
      const id = wrap.dataset.id;
      const p = posOf(id);
      if (!p) return;
      drag = { id, sx: e.clientX, sy: e.clientY, ox: p.x, oy: p.y, moved: false };
      window.addEventListener('pointermove', onDragMove);
      window.addEventListener('pointerup', onDragEnd);
      window.addEventListener('pointercancel', onDragEnd);
    };
    const onDragMove = (e) => {
      if (!drag) return;
      const dx = e.clientX - drag.sx, dy = e.clientY - drag.sy;
      if (!drag.moved) {
        if (Math.hypot(dx, dy) < 4) return;
        drag.moved = true;
        closeHovers();
        S.nodes.get(drag.id)?.el.classList.add('is-dragging');
      }
      const k = vp.view.k;
      moveNode(drag.id, { x: drag.ox + dx / k, y: drag.oy + dy / k });
    };
    const onDragEnd = () => {
      window.removeEventListener('pointermove', onDragMove);
      window.removeEventListener('pointerup', onDragEnd);
      window.removeEventListener('pointercancel', onDragEnd);
      if (!drag) return;
      const d = drag; drag = null;
      const n = S.nodes.get(d.id);
      if (n) n.el.classList.remove('is-dragging');
      if (d.moved) {
        const p = S.pos.get(d.id);
        moveNode(d.id, { x: snap(p.x, 8), y: snap(p.y, 8) });
        savePos(d.id);
        suppressClick = true;
        later(() => { suppressClick = false; }, 0);
      }
    };

    // ---- drawing a link from the port ---------------------------------------------------------------
    const nodeIdAt = (cx, cy) => {
      const el = document.elementFromPoint(cx, cy);
      const wrap = el && el.closest && el.closest('.onode-wrap');
      return wrap && wrap.dataset.id;
    };
    function updateDraft(cx, cy) {
      if (!S.mode) return;
      const from = posOf(S.mode.sup);
      if (!from) return;
      const over = nodeIdAt(cx, cy);
      for (const [id, n] of S.nodes) n.el.classList.toggle('is-over', id === over && id !== S.mode.sup);
      const valid = over && over !== S.mode.sup && !store.linkProblem(S.mode.sup, over);
      layer.setDraft({ from, to: vp.toWorld(cx, cy), snap: valid ? posOf(over) : null });
      return over;
    }
    let linkDrag = null;
    const onPortDown = (e) => {
      const port = e.target.closest('.onode__port');
      if (!port || e.button !== 0) return;
      const id = port.closest('.onode-wrap').dataset.id;
      e.preventDefault(); e.stopPropagation();
      closeHovers();
      linkDrag = { sup: id, sx: e.clientX, sy: e.clientY, moved: false };
      S.mode = { kind: 'drag', sup: id };
      renderStates(); renderBar();
      window.addEventListener('pointermove', onPortMove);
      window.addEventListener('pointerup', onPortUp);
      window.addEventListener('pointercancel', onPortCancel);
    };
    const onPortMove = (e) => {
      if (!linkDrag) return;
      if (!linkDrag.moved && Math.hypot(e.clientX - linkDrag.sx, e.clientY - linkDrag.sy) < 4) return;
      linkDrag.moved = true;
      updateDraft(e.clientX, e.clientY);
    };
    const endPortDrag = () => {
      window.removeEventListener('pointermove', onPortMove);
      window.removeEventListener('pointerup', onPortUp);
      window.removeEventListener('pointercancel', onPortCancel);
    };
    const onPortCancel = () => { endPortDrag(); linkDrag = null; cancelMode(); };
    const onPortUp = (e) => {
      endPortDrag();
      const d = linkDrag; linkDrag = null;
      if (!d) return;
      if (!d.moved) { // a plain click on the port starts click mode
        S.mode = { kind: 'click', sup: d.sup };
        renderStates(); renderBar();
        say('Выберите подопечного: щёлкните по агенту. Escape отменяет');
        return;
      }
      const target = nodeIdAt(e.clientX, e.clientY);
      cancelMode();
      if (target && target !== d.sup) tryLink(d.sup, target);
    };

    // ---- clicks on nodes ------------------------------------------------------------------------------
    let suppressClick = false;
    const onWorldClick = (e) => {
      if (e.target.closest('.onode__port')) return;
      const wrap = e.target.closest('.onode-wrap');
      if (!wrap) return;
      if (suppressClick) return;
      const id = wrap.dataset.id;
      if (S.mode && S.mode.kind === 'click') {
        if (id === S.mode.sup) { cancelMode(); return; }
        if (tryLink(S.mode.sup, id)) cancelMode();
        return;
      }
      setSel({ type: 'agent', id });
      drawer.open(id);
      relayout();
      ensureVisible(id);
    };
    const onNodeKey = (e) => {
      const body = e.target.closest && e.target.closest('.onode');
      if (!body || (e.key !== 'Enter' && e.key !== ' ')) return;
      e.preventDefault();
      body.click();
    };
    world.addEventListener('pointerdown', onNodeDown);
    world.addEventListener('pointerdown', onPortDown, true);
    world.addEventListener('click', onWorldClick);
    world.addEventListener('pointerover', (e) => {
      const wrap = e.target.closest && e.target.closest('.onode-wrap');
      const id = wrap ? wrap.dataset.id : null;
      if (id === S.hover || (drag && drag.moved)) return;
      S.hover = id;
      applyFocus();
    });
    viewportEl.addEventListener('pointerleave', () => { if (S.hover && !(drag && drag.moved)) { S.hover = null; applyFocus(); } });
    world.addEventListener('keydown', onNodeKey);
    viewportEl.addEventListener('pointermove', (e) => { if (S.mode && S.mode.kind === 'click') updateDraft(e.clientX, e.clientY); });
    viewportEl.addEventListener('pointerleave', () => { if (S.mode && S.mode.kind === 'click') layer.setDraft(null); });

    // ---- keyboard ---------------------------------------------------------------------------------------
    const typing = (t) => t && (t.closest ? t.closest('input, textarea, select, [contenteditable="true"], [role="combobox"], [role="listbox"], [role="radio"]') : null);
    const onKey = (e) => {
      if (document.querySelector('.modal, .popover')) return;
      if (e.key === 'Escape') {
        if (S.mode) { e.preventDefault(); cancelMode(); return; }
        if (drawer.isOpen) {
          if (document.activeElement && drawer.el.contains(document.activeElement)) document.activeElement.blur();
          e.preventDefault(); drawer.close(); return;
        }
        if (S.sel) { setSel(null); }
        return;
      }
      if (typing(e.target) || e.ctrlKey || e.metaKey || e.altKey) return;
      if (e.code === 'KeyF') { e.preventDefault(); fit(true); return; }
      if (e.key === 'Delete' || e.key === 'Backspace') {
        if (!S.sel) return;
        e.preventDefault();
        if (S.sel.type === 'edge') removeEdge(store.get('edges').find((x) => x.id === S.sel.id));
        else { const a = selectedAgent(); if (a) deleteAgent(a); }
        return;
      }
      if (e.key.startsWith('Arrow') && S.sel && S.sel.type === 'agent') {
        const p = posOf(S.sel.id);
        if (!p) return;
        e.preventDefault();
        const step = e.shiftKey ? 32 : 8;
        moveNode(S.sel.id, { x: p.x + (e.key === 'ArrowRight' ? step : e.key === 'ArrowLeft' ? -step : 0), y: p.y + (e.key === 'ArrowDown' ? step : e.key === 'ArrowUp' ? -step : 0) });
        savePos(S.sel.id);
      }
    };
    document.addEventListener('keydown', onKey);

    // ---- the bar over the canvas ----------------------------------------------------------------------
    function renderBar() {
      const a = selectedAgent();
      if (S.mode) {
        const sup = agentOf(S.mode.sup);
        replace(barHost, h('div.orch-bar__box', h('span.orch-bar__text', `Выберите подопечного для «${sup ? sup.name : ''}»`),
          button({ label: 'Отмена', variant: 'quiet', size: 'sm', kbd: 'Esc', onClick: () => cancelMode() })));
      } else if (a) {
        replace(barHost, h('div.orch-bar__box',
          button({ label: 'Назначить подопечного', icon: 'link-2', variant: 'secondary', size: 'sm', onClick: () => startAssign(a.id) }),
          drawer.isOpen && drawer.agentId === a.id ? null : button({ label: 'Свойства', icon: 'panel-right', variant: 'quiet', size: 'sm', onClick: () => { drawer.open(a.id); relayout(); ensureVisible(a.id); } })));
      } else if (S.sel && S.sel.type === 'edge') {
        const e = store.get('edges').find((x) => x.id === S.sel.id);
        const sup = e && agentOf(e.supervisor_id), sub = e && agentOf(e.subordinate_id);
        replace(barHost, e ? h('div.orch-bar__box', h('span.orch-bar__text', `${sup ? sup.name : ''} → ${sub ? sub.name : ''}`),
          button({ label: 'Удалить связь', icon: 'unlink', variant: 'danger', size: 'sm', onClick: () => removeEdge(e) })) : null);
      } else replace(barHost, null);
    }

    // ---- rendering ---------------------------------------------------------------------------------------
    const tracker = createDelegationTracker(store, ({ active, returned }) => {
      for (const key of returned) {
        const [sup, sub] = key.split('>');
        if (!store.edgeBetween(sup, sub)) {
          const t = later(() => { S.lingering.delete(key); scheduleGraph(); }, 1300);
          S.lingering.set(key, { key, sup, sub, t });
        }
      }
      renderGraph();
      for (const key of returned) layer.playReturn(key);
    });

    function renderGraph() {
      const active = tracker.active;
      const ghosts = [];
      for (const [key, v] of active) if (!store.edgeBetween(v.sup, v.sub)) ghosts.push({ key, sup: v.sup, sub: v.sub });
      for (const [key, v] of S.lingering) if (!active.has(key)) ghosts.push({ key, sup: v.sup, sub: v.sub });
      layer.render({
        edges: store.get('edges'), ghosts, pos: posOf,
        selectedId: S.sel && S.sel.type === 'edge' ? S.sel.id : null,
        active: new Set(active.keys()), focus: focusSet(),
        labelFor: (a, b) => `${agentOf(a) ? agentOf(a).name : ''} → ${agentOf(b) ? agentOf(b).name : ''}`,
      });
    }
    let graphRaf = 0;
    function scheduleGraph() { if (!graphRaf && !dead) graphRaf = requestAnimationFrame(() => { graphRaf = 0; renderGraph(); }); }

    function syncNodes() {
      const agents = store.get('agents');
      const ids = new Set(agents.map((a) => a.id));
      for (const [id, n] of S.nodes) if (!ids.has(id)) { n.destroy(); S.nodes.delete(id); S.pos.delete(id); }
      for (const a of agents) {
        let n = S.nodes.get(a.id);
        if (!n) { n = createNode(a); S.nodes.set(a.id, n); nodesLayer.appendChild(n.el); }
        n.update(a);
        n.setPos(posOf(a.id));
      }
    }

    function syncEmpty() {
      const loaded = store.get('loaded');
      const agents = store.get('agents');
      const kind = !loaded ? (store.get('connection') === 'closed' ? 'down' : 'wait') : agents.length ? 'none' : 'empty';
      if (kind !== S.emptyKind) {
        S.emptyKind = kind;
        if (kind === 'empty') {
          replace(emptyHost, noAgents({ onCreate: () => createAgent(), onSeed: seedDemo }));
        } else if (kind === 'down' || kind === 'wait') replace(emptyHost, connecting(kind === 'down'));
        else replace(emptyHost, null);
        stage.classList.toggle('is-empty', kind !== 'none');
      }
    }

    function syncAll() {
      syncEmpty();
      syncNodes();
      renderStates();
      renderGraph();
      composer.sync();
      lane.sync();
      drawer.sync();
      renderBar();
      syncActions();
      if (!S.sel || !(S.sel.type === 'agent' ? agentOf(S.sel.id) : store.get('edges').some((e) => e.id === S.sel.id))) {
        if (S.sel) { S.sel = null; renderStates(); renderBar(); }
      }
      if (!S.fitted && store.get('loaded') && store.get('agents').length) { S.fitted = true; fit(false); }
      relayout();
    }
    function scheduleRender() { if (!S.raf && !dead) S.raf = requestAnimationFrame(() => { S.raf = 0; syncAll(); }); }

    // ---- top bar -----------------------------------------------------------------------------------------
    async function seedDemo() {
      try { await api.post('/demo/seed'); S.fitted = false; toast('Демо-команда загружена', { kind: 'ok' }); } catch (e) { toast(e.message || 'Не удалось загрузить демо', { kind: 'error' }); }
    }
    const newBtn = button({ label: 'Новый агент', icon: 'plus', variant: 'primary', onClick: () => createAgent() });
    const demoBtn = button({ label: 'Убрать демо', icon: 'trash-2', variant: 'quiet', onClick: async () => {
      const ok = await confirm({ title: 'Убрать демо-команду?', message: 'Демо-агенты, их связи и запуски будут удалены. Агенты, которых вы создали сами, останутся.', confirmLabel: 'Убрать демо', danger: true });
      if (!ok) return;
      try { await api.post('/demo/clear'); toast('Демо-команда убрана', { kind: 'ok' }); } catch (e) { toast(e.message || 'Не удалось убрать демо', { kind: 'error' }); }
    } });
    function syncActions() {
      newBtn.update({ disabled: !store.get('loaded') });
      tidyBtn.update({ disabled: !store.get('agents').length });
      demoBtn.hidden = !store.get('agents').some((a) => a.demo);
    }
    const tidyBtn = button({ label: 'Упорядочить', icon: 'workflow', variant: 'secondary', title: 'Выстроить граф по уровням иерархии', onClick: () => autoLayout() });
    ctx.setActions(demoBtn, tidyBtn, newBtn);

    // ---- wire everything up --------------------------------------------------------------------------------
    for (const key of ['agents', 'edges', 'loaded', 'connection', 'runtimes', 'settings']) offs.push(store.subscribe(key, scheduleRender));
    offs.push(store.subscribeRuns(() => { lane.sync(); }));
    offs.push(live.onStatus(() => scheduleRender()));
    tracker.start();
    syncAll();
    store.loadRuns({ status: 'running', limit: 200 }).then(() => lane.sync()).catch(() => {});

    M = {
      unmount() {
        dead = true;
        offs.forEach((f) => f());
        tracker.stop();
        document.removeEventListener('keydown', onKey);
        window.removeEventListener('pointermove', onDragMove); window.removeEventListener('pointerup', onDragEnd); window.removeEventListener('pointercancel', onDragEnd);
        endPortDrag();
        cancelAnimationFrame(S.raf); cancelAnimationFrame(graphRaf);
        for (const id of [...S.dirty.keys()]) flushPos(id);
        for (const t of timers) clearTimeout(t);
        timers.clear();
        for (const n of S.nodes.values()) n.destroy();
        layer.destroy();
        vp.destroy();
        drawer.destroy();
      },
    };
  },
  unmount() { if (M) { M.unmount(); M = null; } },
};
