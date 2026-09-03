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

export async function render(container) {
  container.innerHTML = "";
  container.classList.add("cal-layout");
  selectedDayKey = null;

  const left = el("div", { class: "cal-left" });
  const header = el("div", { class: "view-header" }, [
    el("div", {}, [
      el("h2", { text: "Calendar" }),
      el("div", { class: "sub", text: "Real events, plus due-dated Notes rendered alongside them" }),
    ]),
  ]);

  const createBar = buildCreateBar();

  const nav = el("div", { class: "cal-nav" });
  const monthLabel = el("div", { class: "cal-month-label" });
  const prevBtn = el("button", { class: "btn", text: "‹ Prev" });
  const todayBtn = el("button", { class: "btn", text: "Today" });
  const nextBtn = el("button", { class: "btn", text: "Next ›" });
  const archiveBtn = el("button", { class: "btn", text: "Archive", style: "margin-left:auto;" });
  nav.append(prevBtn, todayBtn, monthLabel, nextBtn, archiveBtn);

  const grid = el("div", { class: "cal-grid glass" });

  left.append(header, createBar, nav, grid);

  const dayPanel = el("div", { class: "glass bracket cal-day-panel" });

  container.append(left, dayPanel);

  prevBtn.addEventListener("click", () => { viewMonth.setMonth(viewMonth.getMonth() - 1); renderGrid(monthLabel, grid, createBar, dayPanel); });
  nextBtn.addEventListener("click", () => { viewMonth.setMonth(viewMonth.getMonth() + 1); renderGrid(monthLabel, grid, createBar, dayPanel); });
  // Real bug found live (David: "Today" didn't bring you to the actual
  // day") — this only ever reset the month, it never set selectedDayKey or
  // showed today in the panel, so the grid re-rendered on the current
  // month but nothing was actually selected. Now it jumps to the current
  // month AND selects/shows today, same as clicking today's cell would.
  todayBtn.addEventListener("click", () => {
    const today = new Date();
    viewMonth = new Date(today.getFullYear(), today.getMonth(), 1);
    selectedDayKey = today.toDateString();
    renderGrid(monthLabel, grid, createBar, dayPanel);
  });
  archiveBtn.addEventListener("click", () => openArchiveModal(() => renderGrid(monthLabel, grid, createBar, dayPanel)));

  await renderGrid(monthLabel, grid, createBar, dayPanel);
}

function buildCreateBar() {
  const bar = el("div", { class: "glass card cal-create-bar" });
  const titleInput = el("input", { placeholder: "Event title...", style: "flex:1;" });
  const dateInput = el("input", { type: "date" });
  const startInput = el("input", { type: "time", value: "09:00" });
  const endInput = el("input", { type: "time", value: "09:30" });
  const allDayInput = el("input", { type: "checkbox" });
  const addBtn = el("button", { class: "btn", text: "Create Event" });

  const allDayLabel = el("label", { class: "cal-checkbox-label" }, [allDayInput, "All day"]);

  // Labeled fields instead of a bare placeholder-only input row (audit
  // 2026-09-03) — the time inputs especially were unlabeled and ambiguous.
  const startField = el("div", { class: "field" }, [el("label", { text: "Start" }), startInput]);
  const endField = el("div", { class: "field" }, [el("label", { text: "End" }), endInput]);
  allDayInput.addEventListener("change", () => {
    startField.style.display = allDayInput.checked ? "none" : "";
    endField.style.display = allDayInput.checked ? "none" : "";
  });

  bar.append(el("div", { class: "form-grid" }, [
    el("div", { class: "field field-grow" }, [el("label", { text: "Event title" }), titleInput]),
    el("div", { class: "field" }, [el("label", { text: "Date" }), dateInput]),
    startField,
    endField,
    el("div", { class: "field" }, [el("label", { text: " " }), allDayLabel]),
    addBtn,
  ]));
  bar._dateInput = dateInput; // exposed so day-cell clicks can prefill it
  bar._addBtn = addBtn;
  bar._titleInput = titleInput;
  bar._startInput = startInput;
  bar._endInput = endInput;
  bar._allDayInput = allDayInput;

  return bar;
}

async function renderGrid(monthLabel, grid, createBar, dayPanel) {
  monthLabel.textContent = viewMonth.toLocaleDateString(undefined, { month: "long", year: "numeric" });

  const firstOfMonth = new Date(viewMonth);
  const gridStart = new Date(firstOfMonth);
  gridStart.setDate(gridStart.getDate() - gridStart.getDay()); // back up to the Sunday on/before the 1st

  const gridEnd = new Date(gridStart);
  gridEnd.setDate(gridEnd.getDate() + 42); // always render 6 full weeks for a stable grid height

  const items = await api(`/api/calendar/events?start=${encodeURIComponent(gridStart.toISOString())}&end=${encodeURIComponent(gridEnd.toISOString())}`);

  const byDay = {};
  for (const item of items) {
    const key = localDayKey(item);
    (byDay[key] = byDay[key] || []).push(item);
  }

  grid.innerHTML = "";
  for (const wd of WEEKDAYS) grid.appendChild(el("div", { class: "cal-weekday", text: wd }));

  const cursor = new Date(gridStart);
  const currentMonth = firstOfMonth.getMonth();
  const todayKey = new Date().toDateString();
  const refresh = () => renderGrid(monthLabel, grid, createBar, dayPanel);

  createBar._addBtn.onclick = async () => {
    const title = createBar._titleInput.value.trim();
    if (!title || !createBar._dateInput.value) return;
    let start, end;
    if (createBar._allDayInput.checked) {
      start = new Date(`${createBar._dateInput.value}T00:00:00`).toISOString();
      end = new Date(`${createBar._dateInput.value}T23:59:59`).toISOString();
    } else {
      start = new Date(`${createBar._dateInput.value}T${createBar._startInput.value || "00:00"}`).toISOString();
      end = new Date(`${createBar._dateInput.value}T${createBar._endInput.value || "23:59"}`).toISOString();
    }
    await api("/api/calendar/events", {
      method: "POST",
      body: JSON.stringify({ title, start, end, all_day: createBar._allDayInput.checked }),
    });
    createBar._titleInput.value = "";
    await refresh();
  };

  for (let i = 0; i < 42; i++) {
    const dayKey = cursor.toDateString();
    const isOtherMonth = cursor.getMonth() !== currentMonth;
    const cell = el("div", { class: "cal-day" + (isOtherMonth ? " other-month" : "") + (dayKey === todayKey ? " today" : "") + (dayKey === selectedDayKey ? " selected" : "") });
    cell.appendChild(el("div", { class: "cal-day-num", text: String(cursor.getDate()) }));

    const dayItems = byDay[dayKey] || [];
    for (const item of dayItems.slice(0, 2)) {
      cell.appendChild(el("div", { class: "cal-event-pill " + (item.source === "note" ? "note" : "event") + (item.completed ? " completed" : ""), text: item.title, title: item.title }));
    }
    const overflow = dayItems.length - 2;
    if (overflow > 0) cell.appendChild(el("div", { class: "cal-event-pill overflow", text: `+${overflow} more` }));

    const dateForForm = cursor.toISOString().slice(0, 10);
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
    await renderUpcomingPanel(dayPanel, refresh);
  }
}

async function renderUpcomingPanel(dayPanel, onChange) {
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const weekEnd = new Date(today);
  weekEnd.setDate(weekEnd.getDate() + 7);

  const items = await api(`/api/calendar/events?start=${encodeURIComponent(today.toISOString())}&end=${encodeURIComponent(weekEnd.toISOString())}`);

  dayPanel.innerHTML = "";
  dayPanel.appendChild(el("div", { class: "cal-day-panel-title", text: "Next 7 Days" }));

  if (items.length === 0) {
    dayPanel.appendChild(el("div", { class: "empty-state", text: "Nothing coming up." }));
    return;
  }

  const byDay = {};
  for (const item of items) {
    const key = localDayKey(item);
    (byDay[key] = byDay[key] || []).push(item);
  }

  const cursor = new Date(today);
  for (let i = 0; i < 7; i++) {
    const key = cursor.toDateString();
    const dayItems = byDay[key];
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
  const checkbox = el("input", { type: "checkbox" });
  checkbox.checked = !!item.completed;
  checkbox.addEventListener("change", async () => {
    if (item.source === "calendar") {
      await api(`/api/calendar/events/${item.id}`, { method: "PATCH", body: JSON.stringify({ completed: checkbox.checked }) });
    } else {
      await api(`/api/notes/${item.id}`, { method: "PATCH", body: JSON.stringify({ completed: checkbox.checked }) });
    }
    await refreshGrid();
  });

  const canDelete = item.source === "calendar";
  const trailing = canDelete
    ? el("div", { class: "row-actions" }, [
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
    : el("span", { class: "meta", text: "From Notes" });

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
