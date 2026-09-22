// Where the usage notch's window goes. Pure functions over plain display
// objects ({id, workArea: {x, y, width, height}}), with no Electron import, so
// scripts/test_overlay_placement.cjs can check every edge and display without a
// screen; electron/main.js passes in Electron's screen.getAllDisplays().

const EDGES = ["right", "left", "top", "bottom"];

// CodeNotch's window sizes (notch_window_size in its main.rs): upright it is
// NOTCH_W wide and NOTCH_LONG tall, room for the 70 px pill and the 246 px
// card beside it; lying flat on the top or bottom it is NOTCH_LONG square,
// because the card opens below or above the pill there.
const NOTCH_W = 360;
const NOTCH_LONG = 650;

function isVertical(edge) { return edge === "left" || edge === "right"; }

function sizeFor(edge) {
  return isVertical(edge) ? { width: NOTCH_W, height: NOTCH_LONG } : { width: NOTCH_LONG, height: NOTCH_LONG };
}

// The chosen display, or the primary one when none is chosen or it has been unplugged.
function chooseDisplay(displays, displayId, primaryId) {
  return displays.find((d) => d.id === displayId) || displays.find((d) => d.id === primaryId) || displays[0];
}

// Flush against the edge of the display's work area (so the taskbar is never
// covered), `offset` of the way along that edge: 0 is the top or left end, 1
// the bottom or right end. The window never extends past the work area.
function boundsFor(config, display) {
  const area = display.workArea;
  const edge = EDGES.includes(config.edge) ? config.edge : "right";
  const offset = Number.isFinite(config.offset) ? Math.min(1, Math.max(0, config.offset)) : 0.5;
  const size = sizeFor(edge);
  const width = Math.min(size.width, area.width), height = Math.min(size.height, area.height);
  const along = (room) => Math.round(Math.max(0, room) * offset);
  switch (edge) {
    case "left": return { x: area.x, y: area.y + along(area.height - height), width, height };
    case "top": return { x: area.x + along(area.width - width), y: area.y, width, height };
    case "bottom": return { x: area.x + along(area.width - width), y: area.y + area.height - height, width, height };
    default: return { x: area.x + area.width - width, y: area.y + along(area.height - height), width, height };
  }
}

// The offset a window at `bounds` sits at along its edge: the inverse of
// boundsFor, used when the move handle lets go.
function offsetOf(edge, bounds, display) {
  const area = display.workArea;
  const value = isVertical(edge) ? (bounds.y - area.y) / Math.max(1, area.height - bounds.height)
    : (bounds.x - area.x) / Math.max(1, area.width - bounds.width);
  return Math.min(1, Math.max(0, value));
}

// A drag along the edge: the window follows the pointer on that edge's axis only,
// clamped inside the work area.
function dragTo(edge, bounds, display, drag, point) {
  const area = display.workArea;
  if (isVertical(edge)) {
    const y = Math.min(area.y + area.height - bounds.height, Math.max(area.y, drag.window + (point.y - drag.start)));
    return { ...bounds, y: Math.round(y) };
  }
  const x = Math.min(area.x + area.width - bounds.width, Math.max(area.x, drag.window + (point.x - drag.start)));
  return { ...bounds, x: Math.round(x) };
}

module.exports = { EDGES, NOTCH_W, NOTCH_LONG, isVertical, sizeFor, chooseDisplay, boundsFor, offsetOf, dragTo };
