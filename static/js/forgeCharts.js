import { el } from './api.js';

const svg = (tag, attrs = {}) => {
  const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  return node;
};
export function statCard(label, value, delta) {
  return el('div', { class: 'forge-stat' }, [el('span', { class: 'meta', text: label }),
    el('strong', { text: Number(value).toLocaleString() }),
    el('small', { class: 'forge-delta', text: `${delta >= 0 ? '+' : ''}${Number(delta).toLocaleString()} vs previous 30 days` })]);
}
export function bars(rows, label = 'Commits') {
  const total = rows.reduce((n, r) => n + r.commits, 0);
  const figure = el('figure', { class: 'forge-bar-chart' });
  const chart = svg('svg', { viewBox: '0 0 720 170', role: 'img', 'aria-label': `${label}: ${total} commits over ${rows.length} periods.` });
  const tooltip = el('div', { class: 'forge-chart-tooltip', role: 'status', hidden: true });
  const max = Math.max(1, ...rows.map(r => r.commits)), step = 720 / Math.max(1, rows.length);
  for (const [index, row] of rows.entries()) {
    const text = `${row.date}: ${row.commits.toLocaleString()} commits${row.added !== undefined ? `, +${row.added.toLocaleString()} / -${row.removed.toLocaleString()} lines` : ''}`;
    const group = svg('g', { tabindex: '0', role: 'graphics-symbol', 'aria-label': text, class: 'forge-bar' });
    group.append(svg('rect', { x: index * step + 2, y: 0, width: Math.max(1, step - 4), height: 150, class: 'forge-bar-target' }));
    const height = Math.max(2, row.commits / max * 130);
    group.append(svg('rect', { x: index * step + 2, y: 150 - height, width: Math.max(1, step - 4), height, rx: 2 }));
    const title = svg('title'); title.textContent = text; group.append(title);
    const show = () => { tooltip.textContent = text; tooltip.hidden = false; };
    const hide = () => { tooltip.hidden = true; };
    group.addEventListener('mouseenter', show); group.addEventListener('focus', show);
    group.addEventListener('mouseleave', hide); group.addEventListener('blur', hide);
    chart.append(group);
  }
  figure.append(chart, tooltip, el('figcaption', { class: 'forge-chart-caption', text: rows.length ? `${rows[0].date} to ${rows.at(-1).date}` : 'No commits yet' }));
  return figure;
}
export function donut(rows) {
  const total = rows.reduce((n, r) => n + r.bytes, 0), legend = el('ul', { class: 'forge-legend' });
  const chart = svg('svg', { viewBox: '0 0 160 160', role: 'img', 'aria-label': `Tracked file sizes by extension: ${rows.map(r => `${r.name} ${total ? Math.round(r.bytes / total * 100) : 0}%`).join(', ') || 'No tracked files'}` });
  chart.append(svg('circle', { cx: 80, cy: 80, r: 56, class: 'forge-donut-track' }));
  let offset = 0;
  rows.forEach((row, i) => {
    const share = total ? row.bytes / total * 100 : 0;
    chart.append(svg('circle', { cx: 80, cy: 80, r: 56, fill: 'none', 'stroke-width': 22, pathLength: 100,
      'stroke-dasharray': `${share} ${100 - share}`, 'stroke-dashoffset': -offset, transform: 'rotate(-90 80 80)', style: `stroke:var(--chart-${i + 1})` }));
    offset += share;
    legend.append(el('li', {}, [el('i', { style: `background:var(--chart-${i + 1})`, 'aria-hidden': 'true' }),
      el('span', { text: row.name }), el('strong', { text: `${share.toFixed(1)}%` })]));
  });
  return el('div', { class: 'forge-donut' }, [chart, legend]);
}
export function heatmap(rows) {
  const total = rows.flat().reduce((a, b) => a + b, 0), max = Math.max(1, ...rows.flat());
  const chart = svg('svg', { viewBox: '0 0 600 190', role: 'img', 'aria-label': `Commit rhythm in local time: ${total} commits by weekday and hour.` });
  const days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  rows.forEach((hours, d) => {
    const label = svg('text', { x: 0, y: d * 22 + 15 }); label.textContent = days[d]; chart.append(label);
    hours.forEach((count, h) => {
      const cell = svg('rect', { x: 40 + h * 23, y: d * 22, width: 18, height: 17, rx: 2,
        style: `fill:color-mix(in srgb, var(--accent) ${count ? 25 + count / max * 75 : 0}%, var(--surface-2))` });
      const title = svg('title'); title.textContent = `${days[d]} ${h}:00: ${count} commits`; cell.append(title); chart.append(cell);
    });
  });
  for (const h of [0, 6, 12, 18, 23]) { const text = svg('text', { x: 40 + h * 23, y: 180 }); text.textContent = String(h); chart.append(text); }
  return el('div', { class: 'forge-heatmap' }, [chart, el('div', { class: 'forge-heat-legend' }, [
    el('span', { text: 'Less' }), ...[0, 25, 50, 75, 100].map(n => el('i', { style: `background:color-mix(in srgb,var(--accent) ${n}%,var(--surface-2))`, 'aria-hidden': 'true' })), el('span', { text: `More · local time · peak ${max}` }),
  ])]);
}
export function rankedTable(rows, key, label) {
  const table = el('table', { class: 'forge-ranked', 'aria-label': label });
  table.append(el('thead', {}, [el('tr', {}, ['Name', 'Commits', '+ Lines', '- Lines'].map(text => el('th', { scope: 'col', text })))]),
    el('tbody', {}, rows.map(row => el('tr', {}, [el('th', { scope: 'row', text: row[key] }), ...['commits', 'added', 'removed'].map(k => el('td', { text: Number(row[k]).toLocaleString() }))]))));
  return table;
}
