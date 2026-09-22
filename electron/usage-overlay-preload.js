const { contextBridge, ipcRenderer } = require("electron");

// The notch page's only reach into the main process. Nothing here returns data
// from the system; the page gets its readings from the JARVIS backend.
contextBridge.exposeInMainWorld("usageOverlay", {
  // Whether the pointer is over the pill or card: the window takes clicks only then.
  setInteractive: (on) => ipcRenderer.send("usage-overlay:interactive", !!on),
  openApp: () => ipcRenderer.send("usage-overlay:open-app"),
  hide: () => ipcRenderer.send("usage-overlay:hide"),
  // The move handle: slide the notch along its edge, following the pointer on screen.
  moveStart: (x, y) => ipcRenderer.send("usage-overlay:move-start", { x: Number(x) || 0, y: Number(y) || 0 }),
  moveTo: (x, y) => ipcRenderer.send("usage-overlay:move-to", { x: Number(x) || 0, y: Number(y) || 0 }),
  moveEnd: () => ipcRenderer.send("usage-overlay:move-end"),
  getConfig: () => ipcRenderer.invoke("usage-overlay:config"),
  onConfig: (handler) => ipcRenderer.on("usage-overlay:config", (_event, config) => handler(config)),
});
