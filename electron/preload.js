// electron/preload.js — the only bridge between the sandboxed renderer
// (contextIsolation: true, nodeIntegration: false, in main.js) and Node/OS
// capabilities. Exposes a small, fixed set of narrow functions, not a
// general IPC passthrough — the renderer can ask for exactly these specific
// things and nothing else, matching "trusted gateway, untrusted execution"
// from the harness architecture research (see JARVIS Plan / Harness
// Architecture Ideas).

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("jarvis", {
  pickVaultFolder: () => ipcRenderer.invoke("pick-vault-folder"),
});
