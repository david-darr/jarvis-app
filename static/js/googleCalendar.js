import { api, el, toast } from './api.js';

export function dateOnly(date) {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
}

export function eventColor(node, item) {
  const color = item.calendar_color || item.color;
  if (item.source === 'google' && /^#[a-f\d]{6}$/i.test(color || '')) {
    node.style.setProperty('--calendar-color', color);
    node.classList.add('cal-google-event');
  }
  return node;
}

export function itemsOnDay(items, day) {
  const next = new Date(day); next.setDate(next.getDate() + 1);
  return items.filter(item => {
    if (item.all_day && !item.start.includes('T')) {
      const key = dateOnly(day);
      // Google all-day ends are exclusive; feed/local date semantics stay intact.
      return item.source === 'google' ? item.start <= key && key < item.end : item.start === key;
    }
    const start = new Date(item.start), end = item.end ? new Date(item.end) : new Date(start.getTime() + 1);
    return start < next && end > day;
  });
}

export async function googleCalendars(host, onChange) {
  const auth = await api('/api/auth/status');
  if (!auth.is_admin) return [];
  const status = await api('/api/google/status');
  if (!status.connected) {
    host.append(el('div', { class: 'meta', text: 'Connect Google in Library to add your calendars.' }));
    return [];
  }
  if (!status.calendar_connected) {
    const reconnect = el('button', { class: 'btn', text: 'Reconnect to add Calendar' });
    const onHost = Boolean(window.jarvis?.openGoogleSignIn) || ['127.0.0.1', 'localhost', '::1'].includes(location.hostname);
    reconnect.disabled = !onHost;
    reconnect.onclick = async () => {
      try {
        const result = await api('/api/google/oauth/start', { method: 'POST', body: '{}' });
        if (window.jarvis?.openGoogleSignIn) await window.jarvis.openGoogleSignIn(result.url);
        else window.open(result.url, '_blank', 'noopener');
        reconnect.disabled = true;
        const started = Date.now();
        const poll = async () => {
          if (!host.isConnected) return;
          if (Date.now() - started > 600000) { reconnect.disabled = false; return; }
          try {
            if ((await api('/api/google/status')).calendar_connected) { await onChange(true); return; }
          } catch { /* sign-in may still be in progress */ }
          setTimeout(poll, 1800);
        };
        poll();
      } catch (error) { toast(error.message, 'error'); }
    };
    host.append(reconnect);
    if (!onHost) host.append(el('div', { class: 'meta', text: 'Reconnect on the computer running Kairos.' }));
    return [];
  }
  const calendars = await api('/api/google/calendar/calendars');
  const selected = new Set(status.calendar_ids ?? calendars.filter(c => c.selected).map(c => c.id));
  host.append(el('strong', { text: 'Google calendars' }));
  for (const calendar of calendars) {
    const checkbox = el('input', { type: 'checkbox', checked: selected.has(calendar.id) });
    const label = eventColor(el('label', { class: 'cal-calendar-choice' }, [checkbox, calendar.name]), { source: 'google', color: calendar.color });
    checkbox.addEventListener('change', async () => {
      checkbox.disabled = true;
      checkbox.checked ? selected.add(calendar.id) : selected.delete(calendar.id);
      try {
        await api('/api/google/calendar/settings', { method: 'PATCH', body: JSON.stringify({ calendar_ids: [...selected] }) });
        await onChange();
      } catch {
        checkbox.checked = !checkbox.checked;
        checkbox.checked ? selected.add(calendar.id) : selected.delete(calendar.id);
      } finally { checkbox.disabled = false; }
    });
    host.append(label);
  }
  return calendars;
}

function timeInput(value, zone) {
  const parts = new Intl.DateTimeFormat('en-CA', { timeZone: zone, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).formatToParts(new Date(value));
  const get = key => parts.find(part => part.type === key).value;
  return `${get('year')}-${get('month')}-${get('day')}T${get('hour')}:${get('minute')}`;
}

export function eventDialog(calendars, refresh, item = null, day = new Date(), owner = null) {
  const browserZone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const editingGoogle = item?.source === 'google';
  const title = el('input', { value: item?.title || '', required: true, 'aria-label': 'Event title' });
  const target = el('select', { 'aria-label': 'Calendar', disabled: Boolean(item) }, [
    el('option', { value: 'kairos', text: 'Kairos' }),
    ...calendars.filter(c => ['owner', 'writer'].includes(c.access_role) || c.id === item?.calendar_id)
      .map(c => el('option', { value: c.id, text: c.name })),
  ]);
  target.value = item ? (editingGoogle ? item.calendar_id : 'kairos') : (calendars.find(c => c.primary && ['owner', 'writer'].includes(c.access_role))?.id || 'kairos');
  const allDay = el('input', { type: 'checkbox', checked: Boolean(item?.all_day) });
  const zone = el('input', { value: editingGoogle ? item.time_zone || browserZone : browserZone, 'aria-label': 'Time zone' });
  const zoneField = el('div', { class: 'field' }, [el('label', { text: 'Time zone' }), zone]);
  const start = el('input', { type: 'datetime-local', required: true, 'aria-label': 'Start' });
  const end = el('input', { type: 'datetime-local', required: true, 'aria-label': 'End' });
  const endLabel = el('label', { text: 'End' });
  const location = el('input', { value: item?.location || '', 'aria-label': 'Location' });
  const description = el('textarea', { 'aria-label': 'Description' }); description.value = item?.description || '';
  const feedback = el('div', { class: 'meta', role: 'status' });
  const updateFields = () => {
    const isGoogle = target.value !== 'kairos';
    zoneField.hidden = allDay.checked; zone.disabled = !isGoogle;
    if (!isGoogle) zone.value = browserZone;
    start.type = end.type = allDay.checked ? 'date' : 'datetime-local';
    endLabel.textContent = allDay.checked ? 'End date (exclusive)' : 'End';
    const next = new Date(day); next.setDate(next.getDate() + 1);
    if (allDay.checked) {
      start.value = item?.all_day ? String(item.start).slice(0, 10) : dateOnly(day);
      end.value = editingGoogle && item.all_day ? item.end : dateOnly(next);
    } else {
      start.value = item && !item.all_day ? timeInput(item.start, zone.value) : `${dateOnly(day)}T09:00`;
      end.value = item && !item.all_day && item.end ? timeInput(item.end, zone.value) : `${dateOnly(day)}T09:30`;
    }
  };
  allDay.onchange = updateFields; target.onchange = updateFields;
  updateFields();
  const close = el('button', { type: 'button', class: 'btn quiet', text: 'Cancel' });
  const save = el('button', { type: 'submit', class: 'btn primary', text: item ? 'Save event' : 'Create event' });
  const form = el('form', { class: 'glass modal-panel cal-event-dialog', role: 'dialog', 'aria-modal': 'true', 'aria-label': item ? 'Edit event' : 'Create event' }, [
    el('h3', { text: item ? 'Edit event' : 'Create event' }),
    el('div', { class: 'form-grid' }, [
      el('div', { class: 'field field-grow' }, [el('label', { text: 'Event title' }), title]),
      el('div', { class: 'field' }, [el('label', { text: 'Calendar' }), target]),
      el('div', { class: 'field' }, [el('label', { text: 'Start' }), start]),
      el('div', { class: 'field' }, [endLabel, end]), zoneField,
      el('label', { class: 'cal-checkbox-label' }, [allDay, 'All day']),
      el('div', { class: 'field' }, [el('label', { text: 'Location' }), location]),
      el('div', { class: 'field field-grow' }, [el('label', { text: 'Description' }), description]),
    ]), feedback, el('div', { class: 'row-actions' }, [close, save]),
  ]);
  const backdrop = el('div', { class: 'modal-backdrop' }, [form]);
  const opener = document.activeElement;
  const dispose = () => { backdrop.remove(); document.removeEventListener('keydown', onKey); observer.disconnect(); opener?.focus(); };
  const onKey = event => {
    if (event.key === 'Escape') dispose();
    if (event.key === 'Tab') {
      const nodes = [...form.querySelectorAll('button,input,select,textarea')].filter(n => !n.disabled && n.offsetParent !== null);
      const index = nodes.indexOf(document.activeElement);
      if (event.shiftKey && index <= 0) { event.preventDefault(); nodes.at(-1)?.focus(); }
      else if (!event.shiftKey && index === nodes.length - 1) { event.preventDefault(); nodes[0]?.focus(); }
    }
  };
  const observer = new MutationObserver(() => { if (owner && !owner.isConnected) dispose(); });
  observer.observe(document.body, { childList: true, subtree: true });
  close.onclick = dispose; backdrop.onclick = event => { if (event.target === backdrop) dispose(); };
  form.onsubmit = async event => {
    event.preventDefault(); save.disabled = true;
    try {
      const google = target.value !== 'kairos';
      if (!title.value.trim() || !start.value || !end.value) throw new Error('Enter a title, start and end');
      if (end.value <= start.value) throw new Error('End must be after start');
      if (google) {
        await api('/api/google/calendar/action', { method: 'POST', body: JSON.stringify({
          action: item ? 'update' : 'create', calendar_id: target.value, ...(item ? { event_id: item.id } : {}),
          event: { summary: title.value.trim(), location: location.value, description: description.value,
            start: allDay.checked ? { date: start.value } : { dateTime: `${start.value}:00`, timeZone: zone.value },
            end: allDay.checked ? { date: end.value } : { dateTime: `${end.value}:00`, timeZone: zone.value } },
        }) });
      } else {
        await api(item ? `/api/calendar/events/${item.id}` : '/api/calendar/events', { method: item ? 'PATCH' : 'POST', body: JSON.stringify({
          title: title.value.trim(), start: allDay.checked ? start.value : new Date(start.value).toISOString(),
          end: allDay.checked ? new Date(`${end.value}T00:00:00`).toISOString() : new Date(end.value).toISOString(),
          all_day: allDay.checked, location: location.value, description: description.value,
        }) });
      }
      dispose(); await refresh(); toast(item ? 'Event saved' : 'Event created', 'success');
    } catch (error) { feedback.textContent = error.message; save.disabled = false; }
  };
  document.body.append(backdrop); document.addEventListener('keydown', onKey); title.focus();
}

export function weekGrid(grid, start, items, selectDay, edit) {
  grid.classList.add('cal-week-grid');
  const header = el('div', { class: 'cal-week-header' }, [el('span', { text: 'All day' })]);
  const timeline = el('div', { class: 'cal-week-timeline' });
  const hours = el('div', { class: 'cal-week-hours' });
  // The locale's own hour format, as the rest of the calendar shows times ("9 PM", not "21:00").
  const hourLabel = (hour) => new Date(2000, 0, 1, hour).toLocaleTimeString([], { hour: 'numeric' });
  for (let hour = 0; hour < 24; hour++) hours.append(el('span', { text: hourLabel(hour), style: `top:${hour * 42}px` }));
  timeline.append(hours);
  const today = new Date();
  for (let day = 0; day < 7; day++) {
    const date = new Date(start); date.setDate(date.getDate() + day);
    const next = new Date(date); next.setDate(next.getDate() + 1);
    const dayItems = itemsOnDay(items, date);
    const cell = el('div', { class: 'cal-week-heading' }, [el('button', { type: 'button', class: 'btn quiet',
      text: date.toLocaleDateString(undefined, { weekday: 'short', day: 'numeric' }), onclick: () => selectDay(date, dayItems) })]);
    for (const item of dayItems.filter(item => item.all_day)) cell.append(eventColor(el('button', { type: 'button', class: 'cal-event-pill', text: item.title,
      onclick: () => edit(item, date, dayItems) }), item));
    header.append(cell);
    const column = el('div', { class: 'cal-week-day', 'aria-label': date.toDateString() });
    const minutes = stamp => stamp <= date ? 0 : stamp >= next ? 1440 : stamp.getHours() * 60 + stamp.getMinutes();
    const events = dayItems.filter(item => !item.all_day).map(item => ({ item, from: minutes(new Date(item.start)),
      to: minutes(item.end ? new Date(item.end) : new Date(new Date(item.start).getTime() + 1800000)) })).sort((a, b) => a.from - b.from);
    const lanes = [];
    for (const event of events) {
      let lane = lanes.findIndex(end => end <= event.from);
      if (lane < 0) lane = lanes.length;
      lanes[lane] = Math.max(event.from + 15, event.to); event.lane = lane;
    }
    for (const event of events) {
      const node = eventColor(el('button', { type: 'button', class: 'cal-week-event', text: event.item.title, title: event.item.title,
        onclick: () => edit(event.item, date, dayItems) }), event.item);
      node.style.top = `${event.from * .7}px`; node.style.height = `${Math.max(18, (event.to - event.from) * .7)}px`;
      node.style.left = `${event.lane * 100 / lanes.length}%`; node.style.width = `${100 / lanes.length}%`;
      column.append(node);
    }
    if (date.toDateString() === today.toDateString()) column.append(el('div', { class: 'cal-today-line', 'aria-label': 'Current time', style: `top:${(today.getHours() * 60 + today.getMinutes()) * .7}px` }));
    timeline.append(column);
  }
  grid.replaceChildren(header, timeline);
}
