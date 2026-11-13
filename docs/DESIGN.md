# Оркестр — design brief

Design method: **unslop** by Damir Sokolovsky (@heydamir), CC BY 4.0 — the
skill and its tokens are in `docs/design/unslop/` (`SKILL.md`, `tokens.md`,
`signs.md`, `example-dashboard.html`). Read `SKILL.md` passes 1–7 and 9,
`tokens.md` and `signs.md` before writing any UI. They are binding here; this
file sets the direction and the product-specific decisions on top of them.
Where this file is silent, unslop decides. Keep the credit line in
`web/index.html` as an HTML comment and in the README.

The user chose this direction and motion level. Do not drift from it.

## Direction

- **Product.** A panel where one person arranges Claude Code and OpenCode agents
  into a hierarchy, hands them tasks and sees what each one is doing and how
  much context it has left.
- **Character.** Watchful, technical, calm.
- **World.** A dispatcher's room on a night shift: instruments, dials, scopes,
  a wall diagram of lines and stations. Graphite panels, one phosphor trace.
- **Moments.** (1) The orchestra canvas: agents as instruments on a wall
  diagram, links between them, a delegation visibly travelling down a link.
  (2) The run timeline: a delegation tree drawn like a logic-analyser trace,
  one lane per agent. Everything else is quiet chrome.
- **Signature.** The context dial: a thin ring gauge with fine tick marks that
  shows how full an agent's context window is. It appears wherever an agent
  appears (node, hover card, lists, run header) and nowhere else. No other
  ring, radial or donut gauges anywhere in the product.

## Tokens

Dark only. Graphite ramp with a barely cool cast; surfaces must read as grey.

```
--bg-sunken:   #0e0f11   sidebar, canvas backdrop
--bg:          #131416   page
--surface:     #18191c   cards, panels
--raised:      #212327   inputs, active rows, hover cards
--raised-2:    #2a2c31   hover on raised
--hairline:    rgba(236,238,240,.08)
--hairline-2:  rgba(236,238,240,.14)   emphasised borders, focus-adjacent
--text:        #eceef0
--text-2:      #a2a6ad
--text-3:      #838890   meta, captions, icons

--accent:      #36d1c4   phosphor; selection, primary focus, the live trace
--accent-ink:  #0b1f1d   text on accent
--accent-soft: rgba(54,209,196,.12)

--ok:    #7bc47f     --warn:  #e0a84a     --err:   #e5484d

/* runtime identity — used ONLY as a small mark on agents and lane labels */
--rt-claude:   #d9895f   Claude Code
--rt-opencode: #7aa7e8   OpenCode
--rt-demo:     #8d9199   demo
```

- Accent covers a few percent of a screen: the selected agent, the active nav
  row marker, a live delegation, focus rings. Primary buttons are off-white
  (`--text`) with dark text, not accent.
- Context dial colour: `--text-2` arc up to 70 %, `--warn` from 70 %, `--err`
  from 90 %. The track is a hairline; ticks every 10 %.
- No hue gradients, no glows, no coloured shadows, no glass/backdrop blur, no
  emoji, no uppercase tracked labels, no exclamation marks. Depth = surface
  steps + hairlines. A floating hover card may have one soft neutral shadow.

Type: **IBM Plex Sans** (UI) and **IBM Plex Mono** (figures, ids, model names,
token counts, durations), both with Cyrillic, vendored as woff2 under
`web/assets/fonts/` (take them from the `@fontsource` npm packages; verify the
Cyrillic subset files are included; SIL OFL — keep the licence file). Weights
400 and 500, 600 only for rare emphasis. Scale from `tokens.md`: 12 / 13 / 14 /
16 / 20 / 28. `font-variant-numeric: tabular-nums` on all figures. One number
format per kind across the product: tokens `12 480` / `1,2 млн`, cost `$0.42`,
durations `850 мс` / `12,4 с` / `3 мин 05 с`, percentages `64 %`.

Icons: lucide, inline SVG, 16 px, stroke 1.5, colour of the text role. Vendor
only the icons used (from `lucide-static`).

Spacing: 4 px grid, values from `tokens.md`. Radii: controls 8, cards 12,
panels 16, nested radius math respected.

## Layout

- Shell fills the viewport. Sidebar 248 px (`--bg-sunken`): wordmark «Оркестр»
  in Plex Sans 500 with a flat one-colour mark; nav rows 34 px: Оркестр,
  Запуски, Обзор, Модели и железо, Настройки; at the bottom the connection
  state (live dot + «На связи» / «Нет связи») and the detected runtimes.
- Top bar 56 px: page title (names the context, never a greeting), then
  page actions on the right.
- Content fluid, page padding 24–32, capped at 1600 where it is not a canvas.
- Desktop first (the user works on a Linux desktop). Must stay usable down to
  1100 px wide; below that the sidebar collapses to icons.

## Motion (level chosen by the user: meaningful only)

One curve: `cubic-bezier(0.22, 1, 0.36, 1)`. Durations: 150 ms feedback,
200 ms popovers, 360 ms entrances and layout. Transition only colour,
background, border, opacity, transform (and SVG stroke-dashoffset for the dial).

Allowed, because each explains something that is happening:

1. **Delegation in flight** — while a delegation step is running, a short
   bright dash travels along that link from supervisor to subordinate, on a
   loop; when the result comes back, one dash returns upward once.
2. **Context dial** — the arc tweens to its new value (400 ms); crossing a
   colour threshold tweens the colour.
3. **Live state** — a working agent has a small status dot with a soft
   2 s opacity pulse; idle agents are still.
4. **Figures** — a changed number tweens to the new value (300 ms).
5. **Popovers, hover cards, drawers** — fade and rise 6–8 px from their origin.
6. **Canvas editing** — the link being drawn follows the cursor; a valid drop
   target gets the accent hairline; a new link draws itself in once.
7. **One entrance on load** — main content fades and rises with a small stagger.

Not allowed: ambient or decorative loops, pulsing on things that are not live,
bouncy springs, hover scale on cards, animated width/height/top/left,
`transition: all`. Honour `prefers-reduced-motion`: loops become static marks.

## Copy

Russian, sentence case, short labels, no trailing periods on labels and
buttons, no exclamation marks, no hype. Empty states say what to do next in one
line. Demo data is always marked with a quiet text badge «демо».
A change always says what it compares to («+12 % к предыдущему часу»).
