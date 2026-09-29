window.screenGrab.onOverlayImage(image => { document.getElementById("screen").src = image; });
let start = null;
const selection = document.getElementById("selection");
document.addEventListener("pointerdown", event => {
  if (event.button !== 0) return;
  start = { x: event.clientX, y: event.clientY };
  event.target.setPointerCapture(event.pointerId);
});
document.addEventListener("pointermove", event => {
  if (!start) return;
  const x = Math.min(start.x, event.clientX);
  const y = Math.min(start.y, event.clientY);
  const width = Math.abs(event.clientX - start.x);
  const height = Math.abs(event.clientY - start.y);
  selection.style.cssText = `display:block;left:${x}px;top:${y}px;width:${width}px;height:${height}px`;
});
document.addEventListener("pointerup", event => {
  if (!start) return;
  const x = Math.min(start.x, event.clientX);
  const y = Math.min(start.y, event.clientY);
  const width = Math.abs(event.clientX - start.x);
  const height = Math.abs(event.clientY - start.y);
  start = null;
  const w = innerWidth, h = innerHeight;
  if (width > 7 && height > 7) {
    window.screenGrab.selection({ kind:"region", x:x/w, y:y/h, width:width/w, height:height/h });
  } else {
    window.screenGrab.selection({ kind:"window", x:event.clientX/w, y:event.clientY/h });
  }
});
document.addEventListener("keydown", event => { if (event.key === "Escape") window.screenGrab.cancel(); });
