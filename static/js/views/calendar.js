import { dateOnly, eventColor, itemsOnDay, googleCalendars, eventDialog, weekGrid } from '../googleCalendar.js';
import { api, el, toast, confirmDialog, iconButton } from "../api.js";
import { ICONS } from "../icons.js";

// Real visual monthly grid, resized to leave room for a day-detail panel
// (David's ask, 2026-08-31) that lists every event/note for whichever day
// was last clicked, with delete. Still built on the same merged
// /api/calendar/events range endpoint, so real events and due-dated Notes
// both render in-grid and in the detail panel, tagged by source.
//
// Follow-up asks, same day: (1) default day-panel state (nothing selected)
// shows the next 7 days grouped by day, not a dead "click a day" prompt;
// (2) real bug fix — the "Today" button only reset the month, never
// actually selected/showed today; (3) checkboxes to mark an event or note
// complete directly from Calendar, persisted (real calendar events now have
// a `completed` field — see services/calendar_service.py — and it survives
// a CalDAV/iCal re-sync via the feed's own UID).

const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

// Real bug found live (David: synced Canvas iCal items "displayed a day
// early"). All-day items store a bare "YYYY-MM-DD" string (no time/offset —
// see core/dav_client.py's _to_iso for date-only DTSTART values, e.g. every
// Canvas assignment due date). `new Date("2026-09-05")` parses that per the
// ISO 8601 spec as UTC midnight; toDateString() then renders it in the
// browser's LOCAL timezone, which rolls back to the previous day for any
// timezone behind UTC (Eastern US included) — the exact off-by-one David
// hit. Fixed by building the Date from its literal Y/M/D components
// (local, no UTC round-trip) for date-only values instead of handing the
// bare string to `new Date()`. Timed events (which include an actual time
// and usually a "Z") are unaffected and keep the normal parse.
function localDayKey(item) {
  if (item.all_day && !String(item.start).includes("T")) {
    const [y, m, d] = item.start.split("-").map(Number);
    return new Date(y, m - 1, d).toDateString();
  }
  return new Date(item.start).toDateString();
}

let viewMonth = new Date();
viewMonth.setDate(1);
viewMonth.setHours(0, 0, 0, 0);

let selectedDayKey = null;
let viewMode = 'month';
let viewWeek = new Date();
viewWeek.setHours(0, 0, 0, 0);
viewWeek.setDate(viewWeek.getDate() - viewWeek.getDay());

export async function render(container) {
  container.innerHTML = "";
  container.classList.add("cal-layout");
  selectedDayKey = null;

  const left = el("div", { class: "cal-left" });
  const header = el("div", { class: "view-header" }, [
    el("div", {}, [
      el("h2", { text: "Calendar" }),
      el("div", { class: "sub", text: "Make room for what matters. Events and deadlines, together." }),
    ]),
  ]);

  const createBar = buildCreateBar();
  const calendarList = el('div', { class: 'cal-calendar-list' });

  const nav = el("div", { class: "cal-nav" });
  const monthLabel = el("div", { class: "cal-month-label" });
  const prevBtn = el("button", { class: "btn", text: "‹ Prev" });
  const todayBtn = el("button", { class: "btn", text: "Today" });
  const nextBtn = el("button", { class: "btn", text: "Next ›" });
  const archiveBtn = el("button", { class: "btn", text: "Archive", style: "margin-left:auto;" });
  const monthBtn = el('button', { class: 'btn', text: 'Month', 'aria-pressed': String(viewMode === 'month') });
  const weekBtn = el('button', { class: 'btn', text: 'Week', 'aria-pressed': String(viewMode === 'week') });
  nav.append(prevBtn, todayBtn, monthLabel, nextBtn, monthBtn, weekBtn, archiveBtn);

  const grid = el("div", { class: "cal-grid glass" });

  left.append(header, createBar, calendarList, nav, grid);

  const dayPanel = el("div", { class: "glass bracket cal-day-panel" });

  container.append(left, dayPanel);

  const refresh = () => renderGrid(monthLabel, grid, createBar, dayPanel);
  createBar._refresh = refresh;
  createBar._owner = left;
  const setMode = mode => {
    if (mode !== viewMode) {
      if (mode === 'week') {
        const today = new Date();
        const anchor = selectedDayKey ? new Date(selectedDayKey)
          : (viewMonth.getFullYear() === today.getFullYear() && viewMonth.getMonth() === today.getMonth() ? today : viewMonth);
        viewWeek = new Date(anchor.getFullYear(), anchor.getMonth(), anchor.getDate() - anchor.getDay());
      } else {
        const middle = new Date(viewWeek); middle.setDate(middle.getDate() + 3);
        viewMonth = new Date(middle.getFullYear(), middle.getMonth(), 1);
      }
    }
    viewMode = mode;
    monthBtn.setAttribute('aria-pressed', String(mode === 'month'));
    weekBtn.setAttribute('aria-pressed', String(mode === 'week'));
    selectedDayKey = null; refresh();
  };
  monthBtn.onclick = () => setMode('month'); weekBtn.onclick = () => setMode('week');
  const shift = amount => {
    if (viewMode === 'week') viewWeek.setDate(viewWeek.getDate() + amount * 7);
    else viewMonth.setMonth(viewMonth.getMonth() + amount);
    refresh();
  };
  prevBtn.onclick = () => shift(-1); nextBtn.onclick = () => shift(1);
  // Real bug found live (David: "Today" didn't bring you to the actual
  // day") — this only ever reset the month, it never set selectedDayKey or
  // showed today in the panel, so the grid re-rendered on the current
  // month but nothing was actually selected. Now it jumps to the current
  // month AND selects/shows today, same as clicking today's cell would.
  todayBtn.addEventListener("click", () => {
    const today = new Date();
    viewMonth = new Date(today.getFullYear(), today.getMonth(), 1);
    viewWeek = new Date(today.getFullYear(), today.getMonth(), today.getDate() - today.getDay());
    createBar._dateInput.value = dateOnly(today);
    selectedDayKey = today.toDateString();
    renderGrid(monthLabel, grid, createBar, dayPanel);
  });
  archiveBtn.addEventListener("click", () => openArchiveModal(() => renderGrid(monthLabel, grid, createBar, dayPanel)));

  try {
    createBar._calendars = await googleCalendars(calendarList, reconnect => reconnect ? render(container) : refresh());
  } catch (error) { calendarList.append(el('div', { class: 'meta', text: `Google calendars unavailable: ${error.message}` })); }
  dayPanel._createBar = createBar;
  await refresh();
  if (!grid.isConnected) return;
  const timer = setInterval(() => { if (viewMode === 'week') refresh(); }, 60000);
  const observer = new MutationObserver(() => {
    if (!grid.isConnected) { clearInterval(timer); observer.disconnect(); }
  });
  observer.observe(document.body, { childList: true, subtree: true });
}

function buildCreateBar() {
  const bar = el('div', { class: 'cal-create-bar' });
  bar._dateInput = el('input', { type: 'date', value: dateOnly(new Date()) });
  bar._calendars = [];
  bar.append(el('button', { type: 'button', class: 'btn primary', text: '+ Add an event', onclick: () => {
    const day = new Date(`${bar._dateInput.value}T00:00:00`);
    eventDialog(bar._calendars, bar._refresh, null, day, bar._owner);
  } }));
  return bar;
}

async function renderGrid(monthLabel, grid, createBar, dayPanel) {
  monthLabel.textContent = viewMonth.toLocaleDateString(undefined, { month: "long", year: "numeric" });

  const generation = grid._generation = (grid._generation || 0) + 1;
  const mode = viewMode;
  const firstOfMonth = new Date(viewMonth);
  const gridStart = new Date(mode === 'week' ? viewWeek : firstOfMonth);
  gridStart.setDate(gridStart.getDate() - gridStart.getDay()); // back up to the Sunday on/before the 1st

  const gridEnd = new Date(gridStart);
  gridEnd.setDate(gridEnd.getDate() + (mode === 'week' ? 7 : 42)); // always render 6 full weeks for a stable grid height

  const items = await api(`/api/calendar/events?start=${encodeURIComponent(gridStart.toISOString())}&end=${encodeURIComponent(gridEnd.toISOString())}`);

  if (generation !== grid._generation || !grid.isConnected) return;
  const isCurrent = () => generation === grid._generation && grid.isConnected;
  const byDay = {};
  const previousScroll = grid.scrollTop;
  const enteringView = grid._viewMode !== mode;
  grid._viewMode = mode;
  grid.innerHTML = '';
  grid.classList.toggle('cal-week-grid', mode === 'week');
  const refresh = () => renderGrid(monthLabel, grid, createBar, dayPanel);
  if (mode === 'week') {
    monthLabel.textContent = `${gridStart.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })} · Week`;
    weekGrid(grid, gridStart, items, (date, dayItems) => {
      selectedDayKey = date.toDateString(); createBar._dateInput.value = dateOnly(date);
      renderDayPanel(dayPanel, date, dayItems, refresh);
    }, (item, date, dayItems) => {
      if ((item.source === 'google' && item.editable) || item.source === 'calendar')
        eventDialog(createBar._calendars, refresh, item, date, createBar._owner);
      else renderDayPanel(dayPanel, date, dayItems, refresh);
    });
    grid.scrollTop = enteringView ? Math.max(0, (new Date().getHours() - 1) * 42) : previousScroll;
    if (selectedDayKey) renderDayPanel(dayPanel, new Date(selectedDayKey), itemsOnDay(items, new Date(selectedDayKey)), refresh);
    else await renderUpcomingPanel(dayPanel, refresh, isCurrent);
    return;
  }
  for (const wd of WEEKDAYS) grid.appendChild(el('div', { class: 'cal-weekday', text: wd }));
  const cursor = new Date(gridStart);
  const currentMonth = firstOfMonth.getMonth();
  const todayKey = new Date().toDateString();
  for (let i = 0; i < 42; i++) {
    const dayKey = cursor.toDateString();
    const isOtherMonth = cursor.getMonth() !== currentMonth;
    const cell = el("button", { type: "button", "aria-label": dayKey, class: "cal-day" + (isOtherMonth ? " other-month" : "") + (dayKey === todayKey ? " today" : "") + (dayKey === selectedDayKey ? " selected" : "") });
    cell.appendChild(el("div", { class: "cal-day-num", text: String(cursor.getDate()) }));

    const dayItems = byDay[dayKey] = itemsOnDay(items, cursor);
    for (const item of dayItems.slice(0, 2)) {
      cell.appendChild(eventColor(el("div", { class: "cal-event-pill " + (item.source === "note" ? "note" : "event") + (item.completed ? " completed" : ""), text: item.title, title: item.title }), item));
    }
    const overflow = dayItems.length - 2;
    if (overflow > 0) cell.appendChild(el("div", { class: "cal-event-pill overflow", text: `+${overflow} more` }));

    const dateForForm = dateOnly(cursor);
    const dayDate = new Date(cursor);
    cell.addEventListener("click", () => {
      createBar._dateInput.value = dateForForm;
      selectedDayKey = dayKey;
      renderDayPanel(dayPanel, dayDate, dayItems, refresh);
      [...grid.querySelectorAll(".cal-day")].forEach((c) => c.classList.remove("selected"));
      cell.classList.add("selected");
    });

    grid.appendChild(cell);
    cursor.setDate(cursor.getDate() + 1);
  }

  // Default/Today state: show whichever day is selected, or the next-7-days
  // list if nothing is (David's ask 2026-08-31 — the old default was just
  // a dead "click a day" prompt with nothing actually useful in it).
  if (selectedDayKey) {
    for (const [key, dayItems] of Object.entries(byDay)) {
      if (key === selectedDayKey) {
        renderDayPanel(dayPanel, new Date(key), dayItems, refresh);
        return;
      }
    }
    // Selected day has no items but is still a real day (e.g. today with
    // nothing scheduled) — reconstruct its Date from the key itself.
    renderDayPanel(dayPanel, new Date(selectedDayKey), [], refresh);
  } else {
    await renderUpcomingPanel(dayPanel, refresh, isCurrent);
  }
}

async function renderUpcomingPanel(dayPanel, onChange, isCurrent = () => dayPanel.isConnected) {
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const weekEnd = new Date(today);
  weekEnd.setDate(weekEnd.getDate() + 7);

  const items = await api(`/api/calendar/events?start=${encodeURIComponent(today.toISOString())}&end=${encodeURIComponent(weekEnd.toISOString())}`);
  if (!isCurrent()) return;

  dayPanel.innerHTML = "";
  dayPanel.appendChild(el("div", { class: "cal-day-panel-title", text: "Next 7 Days" }));

  if (items.length === 0) {
    dayPanel.appendChild(el("div", { class: "empty-state", text: "Nothing coming up." }));
    return;
  }

  const cursor = new Date(today);
  for (let i = 0; i < 7; i++) {
    const key = cursor.toDateString();
    const dayItems = itemsOnDay(items, cursor);
    if (dayItems && dayItems.length > 0) {
      dayPanel.appendChild(el("div", {
        class: "cal-upcoming-day-label",
        text: i === 0 ? "Today" : cursor.toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" }),
      }));
      for (const item of dayItems) appendItemCard(dayPanel, item, () => renderUpcomingPanel(dayPanel, onChange), onChange);
    }
    cursor.setDate(cursor.getDate() + 1);
  }
}

function renderDayPanel(dayPanel, date, items, onChange) {
  dayPanel.innerHTML = "";
  dayPanel.appendChild(el("div", { class: "cal-day-panel-title", text: date.toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" }) }));

  if (items.length === 0) {
    dayPanel.appendChild(el("div", { class: "empty-state", text: "Nothing on this day." }));
    return;
  }

  for (const item of items) {
    appendItemCard(dayPanel, item, () => renderDayPanel(dayPanel, date, items, onChange), onChange);
  }
}

// Shared by both the day panel and the upcoming list — a checkbox to mark
// an event/note complete (David's ask 2026-08-31), plus delete for
// calendar-sourced items (note-backed ones are deleted from Notes, not
// here). `rerenderSelf` redraws just this panel's own list after a toggle;
// `refreshGrid` also re-fetches the month grid so its pills/dots stay in
// sync (a completed note especially — it disappears from the merged view
// entirely once checked, same as Notes tab's own default filtering).
function appendItemCard(dayPanel, item, rerenderSelf, refreshGrid) {
  const edit = () => eventDialog(dayPanel._createBar?._calendars || [], refreshGrid, item,
    new Date(localDayKey(item)), dayPanel._createBar?._owner);
  if (item.source === 'google') {
    const actions = el('div', { class: 'row-actions' });
    if (item.editable) actions.append(
      el('button', { type: 'button', class: 'btn quiet', text: 'Edit', onclick: edit }),
      iconButton(ICONS.trash, 'Delete event', async () => {
        if (!await confirmDialog({ title: 'Delete this Google event?', message: item.title, confirmLabel: 'Delete event' })) return;
        await api('/api/google/calendar/action', { method: 'POST', body: JSON.stringify({ action: 'delete', calendar_id: item.calendar_id, event_id: item.id }) });
        await refreshGrid(); toast('Event deleted', 'success');
      }, { danger: true }),
    );
    dayPanel.append(eventColor(el('div', { class: 'glass card has-row-actions' }, [
      el('div', { class: 'title', text: item.title }),
      el('div', { class: 'meta', text: `${item.calendar_name} · ${item.all_day ? 'All day' : new Date(item.start).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}` }), actions,
    ]), item));
    return;
  }
  const checkbox = el("input", { type: "checkbox" });
  checkbox.checked = !!item.completed;
  checkbox.addEventListener("change", async () => {
    if (item.source === "calendar") {
      await api(`/api/calendar/events/${item.id}`, { method: "PATCH", body: JSON.stringify({ completed: checkbox.checked }) });
    } else if (item.source === "tab") {
      await api(item.toggle_url, { method: "PATCH", body: JSON.stringify({ completed: checkbox.checked }) });
    } else {
      await api(`/api/notes/${item.id}`, { method: "PATCH", body: JSON.stringify({ completed: checkbox.checked }) });
    }
    await refreshGrid();
  });

  const canDelete = item.source === "calendar";
  const trailing = canDelete
    ? el("div", { class: "row-actions" }, [
        el("button", { type: "button", class: "btn quiet", text: "Edit", onclick: edit }),
        iconButton(ICONS.trash, "Delete event", async () => {
          const ok = await confirmDialog({
            title: "Delete this event?",
            message: `"${item.title}" will be permanently removed from your calendar.`,
            confirmLabel: "Delete event",
          });
          if (!ok) return;
          await api(`/api/calendar/events/${item.id}`, { method: "DELETE" });
          await refreshGrid();
          toast("Event deleted", "success");
        }, { danger: true }),
      ])
    : el("span", { class: "meta", text: item.source === "tab" ? item.source_label : "From Notes" });

  dayPanel.appendChild(el("div", { class: "glass card has-row-actions" + (item.completed ? " cal-item-completed" : "") }, [
    el("div", { class: "card-row" }, [
      checkbox,
      el("div", { style: "flex:1;" }, [
        el("div", { class: "title", text: item.title }),
        el("div", { class: "meta", text: item.all_day ? "All day" : new Date(item.start).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) }),
      ]),
      trailing,
    ]),
  ]));
}

// -- Archive (David's ask, 2026-08-31): a real modal listing every checked-
// off event/note with a Reopen action, not date-range-limited so a
// completed item stays findable regardless of when it was scheduled.
// Reuses the same .modal-backdrop/.modal-panel pattern as the workspace
// picker (static/js/views/chat.js) and Settings window.
let archiveModal = null;

function getArchiveModal() {
  if (archiveModal) return archiveModal;
  const closeBtn = el("button", { class: "modal-close-btn", text: "✕" });
  const body = el("div", { style: "overflow-y:auto;flex:1;margin-top:10px;" });
  const panel = el("div", { class: "glass modal-panel" }, [
    el("h4", { text: "Archive" }, [closeBtn]),
    el("div", { class: "muted", text: "Checked-off events and notes. Reopen to bring one back to the active calendar." }),
    body,
  ]);
  const backdrop = el("div", { class: "modal-backdrop hidden" }, [panel]);
  backdrop.addEventListener("click", (e) => { if (e.target === backdrop) backdrop.classList.add("hidden"); });
  panel.addEventListener("click", (e) => e.stopPropagation());
  closeBtn.addEventListener("click", () => backdrop.classList.add("hidden"));
  document.body.appendChild(backdrop);
  archiveModal = { backdrop, body };
  return archiveModal;
}

async function openArchiveModal(onChange) {
  const modal = getArchiveModal();
  modal.backdrop.classList.remove("hidden");
  await refreshArchive(modal.body, onChange);
}

async function refreshArchive(body, onChange) {
  const [events, notes] = await Promise.all([
    api("/api/calendar/events/archived"),
    api("/api/notes?include_completed=true"),
  ]);
  const completedNotes = notes.filter((n) => n.completed && n.due_date);
  const items = [
    ...events.map((e) => ({ ...e, source: "calendar" })),
    ...completedNotes.map((n) => ({ id: n.id, title: n.text, start: n.due_date, all_day: false, source: "note" })),
  ].sort((a, b) => new Date(b.start) - new Date(a.start));

  body.innerHTML = "";
  if (items.length === 0) {
    body.appendChild(el("div", { class: "empty-state", text: "Nothing archived yet." }));
    return;
  }

  for (const item of items) {
    const reopenBtn = el("button", { class: "btn", text: "Reopen" });
    reopenBtn.addEventListener("click", async () => {
      if (item.source === "calendar") {
        await api(`/api/calendar/events/${item.id}`, { method: "PATCH", body: JSON.stringify({ completed: false }) });
      } else {
        await api(`/api/notes/${item.id}`, { method: "PATCH", body: JSON.stringify({ completed: false }) });
      }
      await refreshArchive(body, onChange);
      await onChange();
    });
    body.appendChild(el("div", { class: "card-row", style: "justify-content:space-between;align-items:center;margin-top:8px;" }, [
      el("div", {}, [
        el("div", { class: "title", style: "font-size:12.5px;", text: item.title }),
        // localDayKey's same all-day-safe date handling — a bare "YYYY-MM-DD"
        // (every all-day/Canvas-style event) must not round-trip through
        // new Date()'s UTC parsing, or it hits the exact off-by-one bug
        // already fixed above for the month grid.
        el("div", { class: "meta", text: `${item.source === "note" ? "Note" : "Event"} · ${new Date(localDayKey(item)).toLocaleDateString()}` }),
      ]),
      reopenBtn,
    ]));
  }
}
