// Minimal hand-written stroke icons (no icon-font/CDN dependency, matching
// Odysseus's own "no build step, no external asset pipeline" frontend
// posture). One per nav item from David's wireframe. Deliberately simple
// geometric shapes, not pixel-perfect iconography — good enough for now,
// worth revisiting once real visual design (not just wireframe structure)
// happens.

const S = 'fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"';

export const ICONS = {
  home: `<svg viewBox="0 0 24 24" ${S}><path d="M3 11l9-7 9 7"/><path d="M5 10v10h14V10"/></svg>`,
  newChat: `<svg viewBox="0 0 24 24" ${S}><circle cx="12" cy="12" r="9"/><path d="M12 8v8M8 12h8"/></svg>`,
  search: `<svg viewBox="0 0 24 24" ${S}><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/></svg>`,
  chats: `<svg viewBox="0 0 24 24" ${S}><path d="M4 5h16v11H8l-4 4V5z"/></svg>`,
  notes: `<svg viewBox="0 0 24 24" ${S}><path d="M6 3h9l5 5v13H6V3z"/><path d="M9 12h6M9 16h6M9 8h3"/></svg>`,
  library: `<svg viewBox="0 0 24 24" ${S}><path d="M5 4v16M9 4v16M14 4l4 16"/></svg>`,
  calendar: `<svg viewBox="0 0 24 24" ${S}><rect x="3" y="5" width="18" height="16" rx="1.5"/><path d="M3 10h18M8 3v4M16 3v4"/></svg>`,
  email: `<svg viewBox="0 0 24 24" ${S}><rect x="3" y="5" width="18" height="14" rx="1.5"/><path d="M4 6l8 7 8-7"/></svg>`,
  tasks: `<svg viewBox="0 0 24 24" ${S}><path d="M5 7h14M5 12h14M5 17h14"/><circle cx="5" cy="7" r="0.8" fill="currentColor"/><circle cx="5" cy="12" r="0.8" fill="currentColor"/><circle cx="5" cy="17" r="0.8" fill="currentColor"/></svg>`,
  brain: `<svg viewBox="0 0 24 24" ${S}><circle cx="12" cy="12" r="8"/><path d="M12 4v16M6 8l12 8M18 8L6 16"/></svg>`,
  cookbook: `<svg viewBox="0 0 24 24" ${S}><path d="M4 4h13a3 3 0 013 3v13H7a3 3 0 01-3-3V4z"/><path d="M4 17a3 3 0 013-3h13"/></svg>`,
  settings: `<svg viewBox="0 0 24 24" ${S}><circle cx="12" cy="12" r="3"/><path d="M19 12a7 7 0 00-.3-2l2-1.5-2-3.4-2.3.9a7 7 0 00-1.7-1L14 2h-4l-.7 2.6a7 7 0 00-1.7 1l-2.3-.9-2 3.4L5.3 10a7 7 0 000 4l-2 1.5 2 3.4 2.3-.9a7 7 0 001.7 1L10 22h4l.7-2.6a7 7 0 001.7-1l2.3.9 2-3.4-2-1.5c.2-.6.3-1.3.3-2z"/></svg>`,
  // Mobile drawer toggle (David's ask 2026-09-01) — hamburger / close.
  menu: `<svg viewBox="0 0 24 24" ${S}><path d="M4 7h16M4 12h16M4 17h16"/></svg>`,
  close: `<svg viewBox="0 0 24 24" ${S}><path d="M6 6l12 12M18 6L6 18"/></svg>`,
  // Developer Mode toggle (David's ask 2026-09-01) — angle brackets, the
  // universal "code" mark.
  devMode: `<svg viewBox="0 0 24 24" ${S}><path d="M8 6l-5 6 5 6M16 6l5 6-5 6"/></svg>`,
  // "+" New Tab nav item (David's ask 2026-09-01, Developer Mode only).
  plus: `<svg viewBox="0 0 24 24" ${S}><path d="M12 5v14M5 12h14"/></svg>`,
  // Row-action + empty-state icons (David's ask 2026-09-03, professional
  // polish pass) — destructive row actions became hover-revealed icon
  // buttons instead of repeated text buttons, and empty states got a real
  // illustration mark instead of a bare sentence.
  trash: `<svg viewBox="0 0 24 24" ${S}><path d="M4 7h16M10 11v6M14 11v6M5 7l1 13h12l1-13M9 7V4h6v3"/></svg>`,
  edit: `<svg viewBox="0 0 24 24" ${S}><path d="M4 20h4L20 8l-4-4L4 16v4z"/><path d="M14 6l4 4"/></svg>`,
  play: `<svg viewBox="0 0 24 24" ${S}><path d="M7 4l12 8-12 8V4z"/></svg>`,
  check: `<svg viewBox="0 0 24 24" ${S}><path d="M20 6L9 17l-5-5"/></svg>`,
  warning: `<svg viewBox="0 0 24 24" ${S}><path d="M12 3l9 17H3l9-17z"/><path d="M12 9v5M12 17v.5"/></svg>`,
  inbox: `<svg viewBox="0 0 24 24" ${S}><path d="M3 13h5l1.5 3h5L16 13h5"/><path d="M4 13l2-8h12l2 8v6H4v-6z"/></svg>`,
};
