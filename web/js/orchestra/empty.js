// Empty states of the canvas: not connected yet, and no agents (the three steps).
import { h } from '../core/dom.js';
import { button, emptyState } from '../ui/index.js';

/** noAgents({ onCreate, onSeed }) -> node. One block: what the screen is for, three steps, two ways to start. */
export function noAgents({ onCreate, onSeed }) {
  const seed = button({ label: 'Загрузить демо-команду', variant: 'secondary', onClick: async () => { seed.update({ loading: true }); try { await onSeed(); } finally { seed.update({ loading: false }); } } });
  const step = (n, title, text) => h('li.orch-step', h('span.orch-step__n.mono', n), h('div', h('div.orch-step__title', title), h('div.orch-step__text', text)));
  return h('div.orch-empty', { dataset: { nopan: '' } }, h('div.orch-empty__panel',
    h('h2.orch-empty__title', 'Соберите команду агентов'),
    h('p.orch-empty__lead', 'Здесь вы выстраиваете иерархию: руководитель получает задачу и поручает её подопечным.'),
    h('ol.orch-steps',
      step('01', 'Создайте агентов', 'Имя, среда запуска, модель и папка проекта'),
      step('02', 'Соедините их связями', 'Протяните линию от порта на кромке руководителя к подопечному'),
      step('03', 'Дайте задачу', 'Выберите агента и опишите, что нужно сделать')),
    h('div.orch-empty__actions',
      button({ label: 'Новый агент', icon: 'plus', variant: 'primary', onClick: onCreate }), seed)));
}

export function connecting(down) {
  return h('div.orch-empty', down
    ? emptyState({ title: 'Нет связи с сервером', text: 'Панель повторяет попытку сама. Если сервер не запущен, запустите его командой python -m orkestr' })
    : emptyState({ title: 'Подключение к серверу', text: 'Загружаем агентов и связи' }));
}
