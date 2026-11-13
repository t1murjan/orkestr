// Lucide icons, inline SVG, 16 px, stroke 1.5, colour of the surrounding text (currentColor).
import { svg } from '../core/dom.js';
import { ICON_DATA } from './icon-data.js';

/** icon('plus') -> <svg>. Names: see ICON_NAMES. */
export function icon(name, { size = 16, stroke = 1.5, className = '' } = {}) {
  const kids = ICON_DATA[name];
  if (!kids) console.warn(`icon: unknown "${name}"`);
  return svg('svg', {
    class: ['icon', className],
    width: size, height: size, viewBox: '0 0 24 24',
    fill: 'none', stroke: 'currentColor', 'stroke-width': stroke,
    'stroke-linecap': 'round', 'stroke-linejoin': 'round',
    'aria-hidden': 'true', focusable: 'false',
  }, (kids || []).map(([tag, attrs]) => svg(tag, attrs)));
}
export const ICON_NAMES = Object.keys(ICON_DATA);
