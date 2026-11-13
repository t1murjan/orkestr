// Folder picker dialog on top of GET /fs/dirs.
import { h } from '../core/dom.js';
import { api } from '../core/api.js';
import { modal } from './overlay.js';
import { button } from './button.js';
import { input } from './form.js';
import { icon } from './icons.js';
import { emptyState } from './data.js';

/** pickFolder({ start, title = 'Выбор папки' }) -> Promise<string | null> (null when cancelled) */
export function pickFolder({ start, title = 'Выбор папки' } = {}) {
  return new Promise((resolve) => {
    let current = null;
    let token = 0;
    const list = h('ul.picker__list', { role: 'listbox', 'aria-label': 'Папки' });
    const pathInput = input({ mono: true, ariaLabel: 'Путь', size: 'sm' });
    const up = button({ icon: 'corner-left-up', label: 'Выше', variant: 'quiet', size: 'sm', disabled: true });
    const pick = button({ label: 'Выбрать эту папку', variant: 'primary', disabled: true, onClick: () => m.close(current) });
    const status = h('div.picker__status', { role: 'status' });

    const go = async (path) => {
      const mine = ++token;
      status.textContent = '';
      list.setAttribute('aria-busy', 'true');
      try {
        const d = await api.get('/fs/dirs', path ? { path } : undefined);
        if (mine !== token) return;
        current = d.path;
        pathInput.value = d.path;
        up.update({ disabled: !d.parent });
        up.onclick = null;
        up.__parent = d.parent;
        pick.update({ disabled: false });
        list.replaceChildren(...(d.dirs.length ? d.dirs.map((x) => h('li', { role: 'option' },
          h('button', { type: 'button', class: 'picker__item', onclick: () => go(x.path) }, icon('folder'), h('span.truncate', x.name), icon('chevron-right', { className: 'picker__go' }))))
          : [h('li.picker__none', 'Вложенных папок нет')]));
        list.scrollTop = 0;
      } catch (e) {
        if (mine !== token) return;
        status.textContent = e.message;
        status.dataset.tone = 'err';
      } finally { if (mine === token) list.removeAttribute('aria-busy'); }
    };
    up.addEventListener('click', () => { if (up.__parent) go(up.__parent); });
    pathInput.input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); go(pathInput.value.trim()); } });

    const m = modal({
      title, width: 560,
      content: h('div.picker', h('div.picker__bar', up, pathInput), list, status),
      footer: [button({ label: 'Отмена', variant: 'quiet', onClick: () => m.close(null) }), pick],
      onClose: (r) => resolve(typeof r === 'string' ? r : null),
    });
    go(start || null);
  });
}
