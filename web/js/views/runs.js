// Запуски: the list (#/runs) and a single run (#/runs/<id>). The pieces live in js/runs/.
import { mountRunList } from '../runs/list.js';
import { mountRunPage } from '../runs/page.js';

let dispose = null;

export default {
  id: 'runs',
  title: 'Запуски',
  layout: 'page',
  mount(root, ctx) {
    if (ctx.params.id) { ctx.setTitle('Запуск'); dispose = mountRunPage(root, ctx); }
    else { ctx.setTitle('Запуски'); dispose = mountRunList(root, ctx); }
  },
  unmount() { if (dispose) dispose(); dispose = null; },
};
