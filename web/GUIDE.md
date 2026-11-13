# Оркестр web: guide for screen engineers

No build step, no framework, no CDN. Plain ES modules, hand-written CSS, everything local. Design source of truth: `docs/DESIGN.md` (binding) and `docs/design/unslop/`. Open `web/dev/kit.html` to see every component and chart live (`python3 -m http.server 8806 --directory web`, then `/dev/kit.html`).

## Rules

1. No new colours. Use tokens (`var(--accent)`, `var(--text-2)`, ...). Neutral chart steps are `--ink-1..4`. No hue gradients, glows, coloured shadows, blur, emoji, uppercase tracked labels, exclamation marks.
2. No new fonts. Plex Sans for text, Plex Mono (`.mono`) for figures, ids, model names, token counts, durations. Weights 400 and 500 only (600 for rare emphasis).
3. Never `innerHTML`, `outerHTML`, `insertAdjacentHTML`. Build DOM with `h()` / `svg()`; text is always a text node.
4. Your CSS goes only into `web/css/views/<id>.css` (already linked in `index.html`). Scope every rule with `.view--<id>` (the shell puts that class on your root). Do not edit `tokens.css`, `base.css`, `components.css`, `shell.css` or any shared JS. Missing something shared: say so, do not fork it.
5. Spacing from the 4 px scale (`--sp-1..16`), radii `--r-ctl` 8 / `--r-card` 12 / `--r-panel` 16; inner radius = outer minus padding.
6. Motion: only `--ease`, `--dur-*`; transition colour, background, border, opacity, transform. Never `transition: all`, never animate width/height/top/left. Loops only for live things (status dot, delegation dash). Respect reduced motion (`reducedMotion()` from `core/motion.js`).
7. Russian, sentence case, no trailing periods on labels and buttons, a change always says what it compares to. Demo data gets the quiet badge `badge({ text: 'демо' })`.
8. One format per kind of figure: use `core/format.js`, never `toLocaleString` or hand-rolled formats.
9. Every interactive element is a real `<button>`/`<a>`/`<input>`; keep the focus ring (`:focus-visible` is global); dialogs go through `modal`/`drawer`.
10. The context dial appears wherever an agent appears and nowhere else. No other ring, radial or donut gauges.
11. Clean up: everything you subscribe to in `mount` (store, live, router, intervals, hover cards, charts' observers need no cleanup) is released in `unmount`.

## Writing a view

`web/js/views/<id>.js` default-exports:

```js
export default {
  id: 'runs',            // matches the route and the css scope .view--runs
  title: 'Запуски',      // top bar title
  layout: 'page',        // 'page' (padding, max width 1600) or 'canvas' (full bleed, no padding, scroll yourself)
  mount(root, ctx) {},   // build your DOM into root; return nothing
  unmount() {},          // release subscriptions
  update(ctx) {},        // optional: same view, other params (e.g. #/runs/a -> #/runs/b); without it the view is remounted
};
```

`ctx = { api, live, store, router, params, query, setTitle(title, sub?), setActions(...nodes) }`.
`params` come from the route (`#/runs/<id>` gives `{ id }`). `query` is the parsed `?a=b` part at mount time; later query changes do not remount, listen with `router.onChange`. `setActions` fills the right side of the top bar (buttons).

Routes (in `core/router.js`): `/` orchestra, `/runs`, `/runs/:id` (both runs), `/overview`, `/infra`, `/settings`. The main content fades and rises once on mount (shell does it, except for `canvas`).

```js
import { h } from '../core/dom.js';
import { card, emptyState } from '../ui/index.js';
let offs = [];
export default {
  id: 'infra', title: 'Модели и железо',
  mount(root, ctx) {
    root.append(card({ title: 'Нагрузка', children: h('div', '...') }));
    offs.push(ctx.store.subscribe('hardware', (hw) => { /* update nodes */ }));
  },
  unmount() { offs.forEach((f) => f()); offs = []; },
};
```

Update existing nodes through their `update()` method (numbers tween, dials sweep) instead of rebuilding them.

## Core modules (`web/js/core/`)

**api.js** `api.get(path, params?)`, `api.post(path, body?)`, `api.patch(path, body)`, `api.del(path, params?)`; path is relative to `/api/v1` (`'/agents'`). Returns parsed JSON. Throws `ApiError` (`.code`, `.message` in Russian, `.status`); codes of CONTRACT §3 plus `network`, `timeout`, `bad_response`, `aborted`. Extra third argument `{ signal, timeout }` on get. `null`/`undefined`/`''` params are dropped from the query.

**live.js** `live.on(type, fn) -> off` (`fn(data, type)`; `'*'` gets everything; types per CONTRACT §5 plus `hello`, `ping`), `live.onStatus(fn) -> off` (`'connecting'|'open'|'closed'`), `live.onResync(fn) -> off` (fired after a reconnect: reload what you show), `live.status`, `live.connected`. Reconnects with backoff 1 s to 30 s and a 45 s silence watchdog. main.js starts it; you do not call `start`.

**store.js** state cache, loaded from `/state`, kept fresh from live events.
- `store.get(key)` for `agents` (array), `edges`, `runtimes`, `servers`, `settings`, `hardware` (latest sample or null), `hardwareSeries` (ring of up to 300 samples, fed by live), `meta` (`{version, demo_seeded}`), `connection` (`'connecting'|'open'|'closed'`), `loaded`, `loadError`.
- `store.subscribe(key, fn) -> off` (`fn(value, key)`, called after changes, coalesced per tick, values are replaced immutably); `store.watch(key, fn)` same but also calls once now.
- Lookups: `agentById(id)`, `agentByName(name)`, `runtimeById(id)`, `subordinatesOf(id)`, `supervisorsOf(id)`, `rootAgents()`, `edgeBetween(sup, sub)`, `linkProblem(sup, sub)` (`null | 'self_edge' | 'duplicate_edge' | 'cycle'`, to check before POST `/edges`), `activeRunCount()`.
- Runs and steps (cache of recent ones): `run(id)`, `steps(runId)` (by `seq`), `recentRuns(limit=100)` (newest first), `treeOf(rootId) -> {runs, steps}`, `subscribeRun(id, fn)` (`fn({run, steps})`), `subscribeTree(rootId, fn)` (`fn({runs, steps})`, any change in the delegation tree, use it for the timeline), `subscribeRuns(fn)` (`fn(runs)` on any change), `loadRun(id) -> {run, tree, steps}` (GET `/runs/{id}` and cache; call it once when opening a run, live events keep it fresh), `loadRuns(params) -> Run[]`, `ingestRun(run)`, `ingestStep(step)`, `loadHardwareHistory(windowS=600)` (prefill `hardwareSeries`), `reload()`.
- Mutations go through `api` (POST/PATCH/DELETE); the live event updates the store, so do not edit the cache yourself.

**router.js** `router.current -> {view, path, params, query}`, `router.navigate('/runs/abc', {replace?})`, `router.href('/runs')` (for `<a href>`), `router.setQuery({status: 'error'}, {replace=true})` (null removes a key), `router.onChange(fn) -> off`.

**dom.js** `h('div.card#id', { class, style: {...} | 'css', on: { click }, onclick, dataset, ref(el), 'aria-label': '...', value, disabled }, ...children)`; children: strings/numbers (text nodes), nodes, arrays, `null`/`false` skipped. `svg(tag, attrs, ...children)`, `frag(...)`, `text(s)`, `replace(el, ...kids)`, `clear(el)`, `on(target, type, fn) -> off`, `qs`, `qsa`, `cx(...classes)`, `uid(prefix)`, `setText(el, v)`, `append(parent, kids)`. Caution: `el.replaceChildren(null)` inserts the text "null"; use `replace()` or filter.

**format.js** (all return `—` for unknown): `fmtInt(12480) -> "12 480"`, `fmtDec(12.43, 1) -> "12,4"`, `fmtTokens(1250000) -> "1,3 млн"`, `fmtCost(0.42) -> "$0.42"`, `fmtDuration(ms) -> "850 мс" | "12,4 с" | "3 мин 05 с" | "1 ч 05 мин"`, `fmtPercent(64) -> "64 %"`, `fmtBytes(n) -> "12,4 ГБ"`, `fmtSigned(12, {digits, unit}) -> "+12 %"`, `pctChange(cur, prev) -> number|null`, `fmtClock(ts) -> "14:05:32"`, `fmtTime`, `fmtDate -> "13 нояб."`, `fmtDateTime`, `fmtRelative(ts) -> "3 мин назад"`, `plural(n, forms)`, `pl(n, FORMS.run) -> "3 запуска"` with `FORMS` (`run agent step call delegation token model server subordinate turn second`), `ctxPercent(used, window)`, `fmtContext(used, window) -> "48 200 / 200 000"`, `initials(name)`, `shortId(id)`, labels `runtimeLabel`, `agentStatusLabel`, `runStatusLabel` and maps `RUNTIME_LABEL AGENT_STATUS_LABEL RUN_STATUS_LABEL STEP_KIND_LABEL ORIGIN_LABEL PERMISSION_LABEL SERVER_KIND_LABEL SERVER_STATE_LABEL`, `NB` (no-break space).

**motion.js** `reducedMotion()`, `ease(t)`, `EASE_CSS`, `tween({duration, onUpdate(e), onDone, delay}) -> {cancel}`, `tweenNumber(el, to, {format, duration=300})` (remembers the shown value on `el`, so repeated calls animate from where it is), `tweenValues(from[], to[], onUpdate(arr), {duration})`, `stagger(nodes, {delay=50, max=6})` (the one entrance), `popIn(el, {dir, distance, duration})`, `popOut(el)`, `travel(pathEl, markEl, {duration=1400, loop=true, reverse=false, onEnd}) -> {stop}` (moves an SVG mark along a path via `transform`; the mark's own geometry is centred on 0,0; for the delegation dash; reverse + `loop:false` is the single return dash; with reduced motion it parks mid-path), `drawIn(pathEl)` (a new link draws itself once).

## Components (`import { ... } from '../ui/index.js'`)

Every factory returns a DOM node; `update(patch)` where it makes sense; inputs expose `.value`.

| Call | Notes |
| --- | --- |
| `button({label, icon, iconRight, variant: 'primary'\|'secondary'\|'quiet'\|'danger', size: 'sm'\|'md'\|'lg', onClick, disabled, loading, title, type, kbd})` | primary is off-white, not accent. `update({label, icon, disabled, loading})` |
| `iconButton({icon, label, onClick, variant='quiet', size, pressed})` | `label` required (aria + title) |
| `input({value, placeholder, type, size, icon, suffix, mono, invalid, disabled, id, ariaLabel, onInput(v,e), onChange(v,e)})` | wrapper node; `.input` native element, `.value`, `.focus()`, `update({invalid, disabled, value})` |
| `textarea({value, rows, placeholder, mono, invalid, onInput(v,e)})` | native `<textarea>` |
| `field({label, hint, error, control, required})` | wires label and aria-describedby; `update({error, hint})` |
| `select({options: [{value, label, hint, disabled, mark}], value, onChange(v), placeholder, disabled, size, mono, ariaLabel})` | custom listbox, keyboard accessible; `.value`, `setOptions(opts)`, `update({disabled, invalid})` |
| `segmented({options: [{value, label, icon, title}], value, onChange(v), size, ariaLabel})` | radiogroup; `.value` |
| `switchControl({checked, onChange(b), label, hint, disabled})` | `.checked` |
| `badge({text, tone: 'neutral'\|'ok'\|'warn'\|'err'\|'accent'})` | quiet mono text with hairline |
| `statusDot({status: 'idle'\|'working'\|'delegating'\|'error'\|'ok', size=8, label})` | `working` and `delegating` pulse (2 s opacity); `update({status, label})` |
| `keycap('Esc')` | `<kbd>` |
| `runtimeMark({runtime: 'claude_code'\|'opencode'\|'demo', size=16, label=false})` | the only place runtime colours appear |
| `brandMark(size)` | the product mark |
| `contextDial({used, window, size: 20\|32\|56\|88, center: 'percent'\|'initials'\|Node\|null, initials})` | see below |
| `tabs({tabs: [{id, label, count}], value, onChange(id), ariaLabel})` | `.value`, `setCount(id, n)` |
| `table({columns, rows, rowKey, selectable, selected, onSelect(row), onRowClick(row), sort, onSort(key, dir), empty, dense, ariaLabel, rowRef(tr,row)})` | column `{key, label, align: 'right' for numbers, width, mono, sortable, render(row) -> Node\|string}`; `update({rows, selected, sort})` reuses row elements by key; arrows move focus, Enter/Space selects |
| `card({title, subtitle, actions, children, padding: 'md'\|'lg'\|'none'})` | `.body`, `.head` |
| `statTile({label, value, format=fmtInt, unit, change})` | `change: {delta (percent) \| cur+prev \| text, vs: 'к предыдущему часу', goodWhen: 'up'\|'down'}`; colours only when `goodWhen` is given; numbers only, no sparklines; `update({value, unit, change})` tweens |
| `emptyState({title, text, icon, action: {label, onClick, ...button props}, secondary, compact, children})` | one line saying what to do next |
| `skeleton({lines, height, width})` | static, nothing loops |
| `codeBlock({code, label, copy=true, maxHeight})` | copy button; `update(code)` |
| `popover({anchor, content, placement='bottom-start', offset, matchWidth, className, onClose}) -> {el, close(), reposition()}` | closes on Escape/outside click |
| `hoverCard(anchor, Node \| () => Node, {placement='right-start', delay=250, closeDelay=160, width}) -> {open, close, destroy}` | follows the anchor, stays open while the pointer is inside, also on keyboard focus; the content node may define `destroy()` |
| `tooltip(anchor, text, {placement, delay=450}) -> destroy` | |
| `modal({title, description, content, footer: Node[], width=480, onClose, dismissable}) -> {close(result), closed: Promise, setContent, setTitle}` | traps focus, Escape closes, focus returns |
| `drawer({title, content, footer, width=480, onClose})` | right side panel, same handle |
| `confirm({title, message, confirmLabel, cancelLabel, danger}) -> Promise<boolean>` | |
| `toast(message, {kind: 'info'\|'ok'\|'error', timeout=4500, action: {label, onClick}})` | |
| `pickFolder({start, title}) -> Promise<string\|null>` | folder picker over `GET /fs/dirs` |
| `agentHoverCard(agent)` | static card node, `update(agent)` |
| `attachAgentHover(anchor, agentIdOrObjectOrFn)` | use on every agent name, node and model chip; with an id it stays live from the store |
| `icon(name, {size=16, stroke=1.5})`, `ICON_NAMES` | Lucide set vendored in `ui/icon-data.js` (45 icons); add one by extending that file with the same `[tag, attrs]` shape from `lucide-static` |

Helpers also exported: `dialLevel(pct)`, `copyText`, `place`, `trapFocus`, `overlayRoot`.

### The context dial

`contextDial({used, window, size, center})` returns a node with `update({used, window, center, initials})` and `.percent`. Arc `--text-2` up to 70 %, `--warn` from 70 %, `--err` from 90 %, ticks every 10 % (lit up to the current value), arc and colour tween (400 ms). Unknown `used` or `window` (null) draws an empty track, never 0 %. Sizes: 20 (ring only), 32 (ring and ticks), 56 and 88 (ticks, minor ticks and centre content; `center: 'percent'` or `'initials'` with `initials: initials(agent.name)`). Update it with `dial.update({used: a.live.context_used, window: a.live.context_window})`.

### Agent hover card

Hover an agent or model anywhere: `attachAgentHover(el, agent.id)`. It shows name, runtime mark, model, dial with `used / window` and percent, current activity (`live.activity`), status, tokens in/out, cost, run count, and keeps updating from the store while open.

## Charts (`import { ... } from '../charts/index.js'`)

SVG, resize with their container, `update()` tweens when the shape is unchanged. One accent series per chart, the rest neutral; give a series `color` only for meaning (`'var(--err)'` for errors). All take plain arrays.

- `lineChart({x: [ts], series: [{id, label, values, color?}], height=280, area=true, accent (series id; default first), format (readout), formatAxis, formatX, yMax, ariaLabel})` hover crosshair, readout, arrow keys when focused; `update({x, series})`.
- `stackedBars({x, series, height=220, accent, format, formatAxis, formatX, ariaLabel})` series stack bottom to top; `update({x, series})`.
- `rankedList({items: [{id, label, value, detail, mark}], format, max, accent: 'first'\|'none', onSelect(item), ariaLabel})` thin bars, leader gets the accent; `update({items, max})`.
- `sparkline({values, width=88, height=24, tone: 'neutral'\|'accent'})` for table cells only; `update({values})`.
- `meter({label, value, max=100, display, hint, warn=70, err=90, tone: 'neutral'\|'accent'})` CPU, RAM, VRAM; `update({value, max, display, hint})`.
- Helpers `niceScale(max)`, `smoothPath(points)`.

## CSS you can rely on

Tokens (`css/tokens.css`): colours `--bg-sunken --bg --surface --raised --raised-2 --hairline --hairline-2 --text --text-2 --text-3 --accent --accent-ink --accent-soft --ok --warn --err (+ -soft) --rt-claude --rt-opencode --rt-demo --ink-1..4`; type `--font-sans --font-mono --fs-12/13/14/16/20/28 --lh-* --fw-regular/medium/strong`; spacing `--sp-1..16`; radii `--r-xs --r-sm --r-ctl --r-card --r-panel`; heights `--h-sm 32 --h-md 36 --h-lg 44`; motion `--ease --dur-fast 150 --dur-pop 200 --dur-num 300 --dur-enter 360 --dur-dial 400`; `--shadow-float`, `--page-pad`, `--page-max`.

Utilities (`base.css`): `.mono .num .muted .text-2 .text-1 .text-xs .text-sm .text-lg .text-xl .fw-medium .truncate .nowrap .visually-hidden .row .stack .wrap .grow .between .start .gap-1/2/3/4/6/8 .grid .cols-2/3/4 .divider .section-title .caption`. Numbers: `font-variant-numeric: tabular-nums` is on by default.

Page rhythm: cards in a grid with `gap: var(--sp-4)`, sections 32 apart, compact stats above one wide main chart, a narrower list or table next to it; do not put two same-size charts side by side. Colour of status: use `statusDot`, not coloured text.

## Check before you hand over

- Zero console errors with and without the backend; screens show empty states when the store is empty.
- Everything works by keyboard; dialogs close on Escape.
- Hover an agent name: the hover card appears. Numbers use `format.js`. No text below 12 px except chart axis labels (11 px mono).
- 1100 px wide still usable.
