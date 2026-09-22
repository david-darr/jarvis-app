const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("usageOverlay", {
  collapse: (collapsed) => ipcRenderer.send("usage-overlay:collapse", !!collapsed),
  hide: () => ipcRenderer.send("usage-overlay:hide"),
  openApp: () => ipcRenderer.send("usage-overlay:open-app"),
});
