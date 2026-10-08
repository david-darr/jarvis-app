// Settings > Layout (David, 2026-10-07): the sidebar's groups and order, which
// tabs show, and the order and visibility of Home's panels. Rows can be
// dragged, or moved with their up and down buttons (keyboard and touch);
// each change saves at once and the sidebar and Home follow (layout.js).
import { el } from '../api.js';
import { group, toggle } from '../settingsKit.js';
import { HOME_SECTIONS, ALWAYS_SHOWN, getLayout, saveLayout, knownTabs, sidebarLayout,
  resetSidebarLayout, resetHomeLayout } from '../layout.js';

const GROUP_TITLES = { main: 'Top', workspace: 'Workspace', intelligence: 'Intelligence' };

export function renderLayoutPanel(body) {
  const sidebarHost = el('div', { class: 'layout-lists' });
  const homeHost = el('div', { class: 'layout-lists' });
  body.replaceChildren(
    group({ title: 'Sidebar', description: 'Drag tabs, or use the arrows, to reorder them or move them between groups. Home always shows.',
      actions: [el('button', { class: 'btn quiet', text: 'Reset sidebar', onclick: () => { resetSidebarLayout(); draw(); } })] }, [sidebarHost]),
    group({ title: 'Home', description: "The parts of Home below its card, in order. Hidden ones come back any time.",
      actions: [el('button', { class: 'btn quiet', text: 'Reset Home', onclick: () => { resetHomeLayout(); draw(); } })] }, [homeHost]),
  );

  // Reads the lists back as the layout and saves it.
  function save() {
    const layout = getLayout();
    for (const list of sidebarHost.querySelectorAll('.layout-list')) layout.groups[list.dataset.group] = [...list.children].map((li) => li.dataset.id);
    layout.hiddenTabs = [...sidebarHost.querySelectorAll('.layout-item.is-hidden')].map((li) => li.dataset.id);
    const homeList = homeHost.querySelector('.layout-list');
    layout.home = [...homeList.children].map((li) => li.dataset.id);
    layout.hiddenHome = [...homeList.querySelectorAll('.layout-item.is-hidden')].map((li) => li.dataset.id);
    saveLayout(layout);
    syncButtons();
  }

  function item(id, label, hidden, { lockShown = false } = {}) {
    const li = el('li', { class: 'layout-item' + (hidden ? ' is-hidden' : ''), draggable: 'true', 'data-id': id });
    const up = el('button', { type: 'button', class: 'layout-move', 'aria-label': `Move ${label} up`, text: '↑', onclick: () => move(li, -1) });
    const down = el('button', { type: 'button', class: 'layout-move', 'aria-label': `Move ${label} down`, text: '↓', onclick: () => move(li, 1) });
    const shown = toggle({ label: `Show ${label}`, checked: !hidden, disabled: lockShown,
      onChange: (on) => { li.classList.toggle('is-hidden', !on); save(); } });
    li.append(el('span', { class: 'layout-handle', 'aria-hidden': 'true', text: '⠿' }), el('span', { class: 'layout-label', text: label }), up, down, shown);
    li.addEventListener('dragstart', (e) => { li.classList.add('is-dragging'); e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', id); });
    li.addEventListener('dragend', () => { li.classList.remove('is-dragging'); save(); });
    return li;
  }

  // Up and down step through the list, and past its end into the next group.
  function move(li, step) {
    const list = li.parentElement;
    const sibling = step < 0 ? li.previousElementSibling : li.nextElementSibling;
    if (sibling) { step < 0 ? sibling.before(li) : sibling.after(li); }
    else {
      const lists = [...li.closest('.layout-lists').querySelectorAll('.layout-list')];
      const next = lists[lists.indexOf(list) + step];
      if (!next) return;
      step < 0 ? next.append(li) : next.prepend(li);
    }
    li.querySelector(step < 0 ? '.layout-move' : '.layout-move:nth-of-type(2)')?.focus();
    save();
  }

  function dropZone(list) {
    list.addEventListener('dragover', (e) => {
      const dragged = list.closest('.layout-lists').querySelector('.is-dragging');
      if (!dragged) return;
      e.preventDefault();
      const after = [...list.children].find((child) => child !== dragged && e.clientY < child.getBoundingClientRect().top + child.offsetHeight / 2);
      if (after) after.before(dragged); else list.append(dragged);
    });
  }

  function list(groupId) {
    const ol = el('ol', { class: 'layout-list', 'data-group': groupId });
    dropZone(ol);
    return ol;
  }

  function draw() {
    const layout = getLayout();
    const labels = new Map(knownTabs().map((t) => [t.id, t.label]));
    sidebarHost.replaceChildren(...sidebarLayout([...labels.keys()], { includeHidden: true }).map((g) => {
      const ol = list(g.id);
      ol.append(...g.ids.map((id) => item(id, labels.get(id), layout.hiddenTabs.includes(id), { lockShown: id === ALWAYS_SHOWN })));
      return el('section', { class: 'layout-group' }, [el('h4', { text: GROUP_TITLES[g.id] || g.label }), ol]);
    }));
    const titles = new Map(HOME_SECTIONS.map((s) => [s.id, s.label]));
    const ol = list('home');
    ol.append(...layout.home.map((id) => item(id, titles.get(id), layout.hiddenHome.includes(id))));
    homeHost.replaceChildren(ol);
    syncButtons();
  }

  // The very first row can't go up, nor the very last down.
  function syncButtons() {
    for (const host of [sidebarHost, homeHost]) {
      const rows = [...host.querySelectorAll('.layout-item')];
      rows.forEach((li, i) => {
        const [up, down] = li.querySelectorAll('.layout-move');
        up.disabled = i === 0; down.disabled = i === rows.length - 1;
      });
    }
  }

  draw();
}

